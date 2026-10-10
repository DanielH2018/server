#!/usr/bin/env python3
"""The one `longhorn_reap.py {backups,snapshots}` entry point that replaces the two reapers (#4345).

The consolidation is a navigation refactor, so every test here holds the new entry point to
what the two old ones did: `--help` answers from any directory, every flag the old parsers
accepted still parses, a fail-closed ABORT still reaches the operator through the subcommand,
and the `--apply` re-run hint names a file that exists. The two census tests hold the layout:
nothing `longhorn_reap_*.py` stays flat in scripts/backup/, and the libraries sit in a
`longhorn_reap_lib/` PEP 420 namespace package. The docs census holds the runbooks to the new
subcommand.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_subcommands.py
"""

import pathlib
import subprocess
import sys

import pytest

from _reap_entrypoint_harness import BACKUP_DIR, _run

REPO = BACKUP_DIR.parents[1]
REAP_ENTRY = BACKUP_DIR / "longhorn_reap.py"
LIB_DIR = BACKUP_DIR / "longhorn_reap_lib"

OLD_ENTRYPOINTS = (
    "longhorn_reap_orphan_backups.py",
    "longhorn_reap_orphan_snapshots.py",
)

# Every flag form the old backups parser (`longhorn_reap_orphan_backups._parse_args`) accepted,
# in combinations its cross-flag checks allow. Each must still parse under `backups`.
BACKUPS_FLAG_SETS = [
    ["--apply"],
    ["--mode", "strays", "--apply-deleted-volumes"],
    ["--mode", "migrated-chain", "--claim", "sonarr-config"],
    ["--mode=seeds", "--claim=valheim-config", "--seed-floor", "2"],
    ["--mode", "seeds", "--seed-floor=2", "--max-deletions", "3"],
    ["--max-deletions=4"],
]


def _tracked(prefix: str) -> list[pathlib.PurePosixPath]:
    out = subprocess.run(
        ["git", "ls-files", "--", prefix],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return [pathlib.PurePosixPath(line) for line in out.splitlines() if line]


# ── --help, from the repo root and from elsewhere ───────────────────────────────────────


@pytest.mark.parametrize("subcommand", ["backups", "snapshots"])
@pytest.mark.parametrize("where", ["repo-root", "elsewhere"])
def test_subcommand_help_exits_zero(subcommand, where, tmp_path):
    cwd = REPO if where == "repo-root" else tmp_path
    proc = subprocess.run(
        [sys.executable, str(REAP_ENTRY), subcommand, "--help"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "--help printed nothing"


# ── every old flag still parses ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("flags", BACKUPS_FLAG_SETS, ids=" ".join)
def test_backups_subcommand_accepts_every_old_flag(flags, tmp_path):
    proc, _calls = _run(
        REAP_ENTRY, ["backups", *flags], {"volumes": []}, tmp_path, admin_readable=True
    )
    assert proc.returncode != 2, proc.stderr
    assert "unknown argument" not in proc.stderr, proc.stderr
    # Parsing succeeded only if the run went on to read the cluster.
    assert "can't open file" not in proc.stderr, proc.stderr


def test_snapshots_subcommand_accepts_apply(tmp_path):
    proc, calls = _run(
        REAP_ENTRY,
        ["snapshots", "--apply"],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode != 2, proc.stderr
    assert "unknown argument" not in proc.stderr, proc.stderr
    assert any(arg == "recurringjobs.longhorn.io" for c in calls for arg in c), (
        proc.stderr
    )


# ── behaviour reaches the operator through the subcommand ───────────────────────────────


def test_backups_subcommand_keeps_the_null_body_abort(tmp_path):
    proc, calls = _run(
        REAP_ENTRY, ["backups"], {"volumes": []}, tmp_path, null_kinds=["volumes"]
    )
    assert proc.returncode == 1, proc.stderr
    assert "ABORT: unparseable volume list" in proc.stderr
    assert not any("delete" in c for c in calls)


def test_snapshots_subcommand_keeps_the_non_integral_env_abort(tmp_path):
    proc, calls = _run(
        REAP_ENTRY,
        ["snapshots"],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
        extra_env={"LONGHORN_REAP_MIN_AGE_DAYS": "3.5"},
    )
    assert proc.returncode == 2, proc.stderr
    assert "LONGHORN_REAP_MIN_AGE_DAYS expects an integer, got: 3.5" in proc.stderr
    assert calls == []


@pytest.mark.parametrize("subcommand", ["backups", "snapshots"])
def test_apply_refusal_names_the_new_entrypoint_and_subcommand(subcommand, tmp_path):
    # The re-run hint is what the operator pastes under sudo; it must name a file that still
    # exists and the subcommand that selects the reaper.
    proc, calls = _run(
        REAP_ENTRY,
        [subcommand, "--apply"],
        {"recurringjobs": [], "volumes": []},
        tmp_path,
    )
    out = proc.stderr + proc.stdout
    assert proc.returncode == 1, out
    assert "sudo %s -B %s %s --apply" % (sys.executable, REAP_ENTRY, subcommand) in out
    assert calls == []


# ── layout census ───────────────────────────────────────────────────────────────────────


def test_no_longhorn_reap_module_remains_flat_in_scripts_backup():
    tracked = _tracked("scripts/backup")
    assert any(p.name == "longhorn_reap.py" for p in tracked), (
        "scripts/backup/longhorn_reap.py is not tracked"
    )
    flat = sorted(
        str(p)
        for p in tracked
        if p.parent == pathlib.PurePosixPath("scripts/backup")
        and p.match("longhorn_reap_*.py")
    )
    assert flat == [], f"move these into longhorn_reap_lib/: {flat}"


def test_libraries_live_in_a_namespace_package_without_an_init():
    tracked = _tracked("scripts/backup/longhorn_reap_lib")
    modules = [p for p in tracked if p.suffix == ".py"]
    assert modules, "scripts/backup/longhorn_reap_lib/ holds no tracked module"
    assert not [p for p in modules if p.name == "__init__.py"], (
        "longhorn_reap_lib must stay a PEP 420 namespace package"
    )


def test_runbooks_name_the_new_subcommand_not_the_old_entrypoints():
    scope = [
        p
        for p in _tracked(".")
        if (
            (
                p.parts[0] == "docs"
                and p.suffix == ".md"
                and p.parts[1] not in ("reference", "archive")
            )
            or p.parts[:2] == (".claude", "skills")
            or p.name == "CLAUDE.md"
        )
    ]
    # Named member: the tiering runbook is where an operator finds the backups reaper.
    tiering = REPO / "docs" / "longhorn-backup-tiering.md"
    assert "longhorn_reap.py" in tiering.read_text(), (
        "docs/longhorn-backup-tiering.md no longer names the reaper entry point"
    )
    stale = sorted(
        f"{p}: {old}"
        for p in scope
        if (REPO / p).is_file()
        for old in OLD_ENTRYPOINTS
        if old in (REPO / p).read_text(errors="replace")
    )
    assert stale == [], f"docs still naming a retired entry point: {stale}"
