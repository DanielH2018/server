"""Tests for `_claude_guard.py`, the bootstrap onto the deployed `claude_guard` package.

This is slice 5's narrowed exit criterion (docs/specs/2026-09-06-claude-guard-design.md,
dotfiles repo): `.claude/hooks/_readonly_tables.py` now imports `SSH_HOSTS` and `_SSH_SECRET`
from `claude_guard.tables` rather than carrying its own copies. The deployed-import test was
red before this change — nothing in this repo's `uv` environment could import `claude_guard`
at all, so the two tables could only ever be duplicates, not the same object.

Run: uv run pytest .claude/hooks
"""

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent
REPO = (
    HOOKS.parent.parent
)  # .claude/hooks/tests -> .claude/hooks -> .claude -> repo root

sys.path.insert(0, str(HOOKS))  # _readonly_tables imports _claude_guard by bare name

import _readonly_tables  # noqa: E402
import claude_guard.tables as _tables  # noqa: E402

# Same hardcoded path and reasoning as test_auto_approve_remote_ssh.py:40-46: `uv` missing
# is one reason to skip. The other is the real deploy target itself: this test spawns a
# SEPARATE subprocess, which starts its own sys.modules and never sees conftest.py's
# in-process stand-in, so on a host that genuinely lacks the dotfiles deploy it can only ever
# fail, not prove anything — the same reasoning as the e2e wrapper tests below.
UV_BIN = Path("/home/ubuntu/.local/bin/uv")
_CLAUDE_GUARD_DIR = Path("~/.local/share/claude-guard").expanduser()

_runnable = pytest.mark.skipif(
    not (UV_BIN.exists() and _CLAUDE_GUARD_DIR.is_dir()),
    reason="uv or the deployed claude_guard package is not present on this machine",
)


@_runnable
def test_deployed_import_reaches_both_trusted_hosts():
    """Run the real invocation path: `uv run python`, hooks dir on the path via PYTHONPATH.

    This is the shape `auto-approve-readonly.sh` actually runs under — `cd
    /home/ubuntu/server && exec uv run --no-sync --quiet python <hooks-dir>/<script>.py`,
    which puts the hooks dir at `sys.path[0]` because that is where the invoked script lives.
    `python -c` has no script file, so `sys.path[0]` is the cwd instead; PYTHONPATH is what
    puts the hooks dir on the path for this invocation.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HOOKS)
    proc = subprocess.run(
        [
            str(UV_BIN),
            "run",
            "--no-sync",
            "--quiet",
            "python",
            "-c",
            "import _claude_guard, claude_guard.tables as t; "
            "print(sorted(t.TRUSTED_SSH_HOSTS))",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "daniel-pi" in proc.stdout
    assert "daniel-server" in proc.stdout


def test_bootstrap_raises_when_the_deploy_is_missing(tmp_path, monkeypatch):
    """No `~/.local/share/claude-guard` means `_claude_guard` raises, not a stale fallback.

    Points HOME at an empty tmp dir, then strips every trace of an already-imported
    `claude_guard`/`_claude_guard` (sys.modules entries and any sys.path entry the real
    bootstrap added) so a prior successful import in this process can't mask the failure.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    saved_path = list(sys.path)
    saved_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "_claude_guard" or name.startswith("claude_guard")
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.path[:] = [p for p in sys.path if not p.rstrip("/").endswith("claude-guard")]
    sys.path.insert(0, str(HOOKS))
    try:
        with pytest.raises(ImportError, match=re.escape(str(tmp_path))):
            importlib.import_module("_claude_guard")
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "_claude_guard" or name.startswith("claude_guard"):
                del sys.modules[name]
        sys.modules.update(saved_modules)


@pytest.mark.skipif(
    not _CLAUDE_GUARD_DIR.is_dir(),
    reason="the deployed claude_guard package is not present, so there is nothing to diff against",
)
def test_the_ci_stand_in_matches_the_deployed_tables(claude_guard_stand_in):
    """conftest.py's stand-in is a second copy by construction; this is what diffs it.

    Skips where the real package is absent (every CI run), since that is exactly where the
    stand-in is the only copy. Goes red on a deployed host the moment `claude_guard.tables`
    moves and the stand-in does not — and `prek run` executes this suite before every commit
    from such a host.
    """
    hosts, secret_re, verbs = claude_guard_stand_in
    assert hosts == frozenset(_tables.TRUSTED_SSH_HOSTS)
    assert secret_re.pattern == _tables.SECRET_PATH_RE.pattern
    assert secret_re.flags == _tables.SECRET_PATH_RE.flags
    assert verbs == frozenset(_tables.REMOTE_READONLY_VERBS)


# The names `_readonly_tables.py` reads off `claude_guard.tables`. CI runs against conftest's
# stand-in, so an import the stand-in lacks fails every test that touches the hook at
# collection -- this pins the two lists together on the CI side, where the diff above skips.
_TABLES_IMPORT = re.compile(r"^from claude_guard\.tables import (.+)$", re.MULTILINE)


def test_the_stand_in_carries_every_name_the_hooks_import_from_the_tables(
    claude_guard_stand_in,
):
    imported = set()
    for hook in HOOKS.glob("*.py"):
        for match in _TABLES_IMPORT.finditer(hook.read_text()):
            imported.update(n.strip() for n in match.group(1).split(","))
    assert {"REMOTE_READONLY_VERBS", "SECRET_PATH_RE"} <= imported  # non-vacuity
    stand_in = {
        "TRUSTED_SSH_HOSTS",
        "SECRET_PATH_RE",
        "REMOTE_READONLY_VERBS",
    }
    assert len(claude_guard_stand_in) == len(stand_in)
    assert imported <= stand_in, imported - stand_in


# --- #2052: TIER1 is derived from the package table, and states only its delta -------------


def test_tier1_is_the_package_table_minus_the_named_delta():
    """The derivation, spelled out: what the package lists bare, less what the server guards
    or refuses, plus what is read-only only locally. Each exclusion set is named so a name
    moving between them is a one-line diff with a reason beside it."""
    tier1 = _readonly_tables.TIER1
    assert (
        tier1
        == (
            frozenset(_tables.REMOTE_READONLY_VERBS)
            - _readonly_tables._GUARDED_LOCALLY
            - _readonly_tables._NOT_ADMITTED
        )
        | _readonly_tables._LOCAL_ONLY
    )
    # Non-vacuity on both halves: readers the table must carry, and the delta it must not.
    assert {"ls", "cat", "grep", "jq", "df"} <= tier1
    assert {"cd", "false", "printenv"} <= tier1
    assert not (
        {"ss", "journalctl", "rg", "sensors", "dmesg", "htop", "nvidia-smi"} & tier1
    )


def test_every_locally_guarded_exclusion_has_a_handler():
    """`_GUARDED_LOCALLY` is the set of package names the server admits through a guard, so
    each must have one -- an entry there with no handler is a name silently dropped."""
    spec = importlib.util.spec_from_file_location(
        "aar_2052", HOOKS / "auto-approve-readonly.py"
    )
    assert spec and spec.loader
    aar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aar)
    assert _readonly_tables._GUARDED_LOCALLY <= set(aar.HANDLERS)


@pytest.mark.skipif(not UV_BIN.exists(), reason="uv is not present on this machine")
def test_the_hook_fails_open_when_the_deploy_is_missing(tmp_path):
    """The shims' contract, one layer down: no package -> one stderr line, exit 0, no stdout.

    `test_hook_shim_fail_open.py` pins this shape for the `.sh` shims' own failures; it walks
    `*.sh` only, so a failure inside the Python they exec is out of its reach. HOME is pointed
    at an empty directory so `~/.local/share/claude-guard` is absent; the process is the real
    entry point under the real `uv run` shape, so nothing in-process (conftest's stand-in
    included) can reach it.
    """
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ssh daniel-server uptime"}}
    )
    proc = subprocess.run(
        [
            str(UV_BIN),
            "run",
            "--no-sync",
            "--quiet",
            "python",
            str(HOOKS / "auto-approve-readonly.py"),
        ],
        cwd=REPO,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "classifier did not run" in proc.stderr
    assert str(tmp_path) in proc.stderr
    assert "Traceback" not in proc.stderr


def test_readonly_tables_ssh_objects_are_claude_guards_own():
    """Identity, not just equal value — a future local redefinition breaks this, not `==`.

    This pins the WIRING, not the values: wherever conftest.py's stand-in is in play (every
    CI run, since CI has no dotfiles deploy), `_tables` IS that stand-in, so the assertion is
    `x is x` regardless of what the real `claude_guard.tables` holds. Only a run with the real
    package deployed exercises the values this identity check is meant to protect.
    """
    assert _readonly_tables.SSH_HOSTS == frozenset(_tables.TRUSTED_SSH_HOSTS)
    assert _readonly_tables._SSH_SECRET is _tables.SECRET_PATH_RE


# --- #1982: a verb guarded on one side of the boundary is never bare on the other ----------------
# The verbs `claude_guard/checks/remote.py` guards before its bare-table lookup. Read from the
# package (`REMOTE_GUARDED_VERBS`, exported for this test by dotfiles PR #521) rather than
# copied: the literal this used to carry went stale the moment the package added a guard.


def boundary_violations(handlers, tier1, remote_verbs, package_guarded):
    """Names guarded on one side and listed bare on the other, each tagged with its side.

    Four instances were found in one sweep on 2026-09-18 (`rg --pre`, `sensors -s` and
    `nvidia-smi` bare in the package; `ss -K` bare in TIER1), fixed by hand, and pinned as
    static lists. This is the derived form.
    """
    server_guards_package_bare = set(handlers) & (
        set(remote_verbs) - set(package_guarded)
    )
    package_guards_server_bare = set(package_guarded) & set(tier1)
    return sorted(
        f"server guards, package lists bare: {v}" for v in server_guards_package_bare
    ) + sorted(
        f"package guards, TIER1 lists bare: {v}" for v in package_guards_server_bare
    )


def test_boundary_check_is_flagged_on_a_guard_missing_from_either_side():
    out = boundary_violations(
        handlers={"sed", "rg"},
        tier1={"ls", "ss"},
        remote_verbs={"rg", "ls"},
        package_guarded={"ss"},
    )
    assert out == [
        "server guards, package lists bare: rg",
        "package guards, TIER1 lists bare: ss",
    ]


@pytest.mark.skipif(
    not _CLAUDE_GUARD_DIR.is_dir(),
    reason="the deployed claude_guard package is not present, so there is no boundary to check",
)
def test_no_verb_is_guarded_on_one_side_of_the_boundary_and_bare_on_the_other():
    """The live check, on a deployed host only; CI's stand-in carries no verb tables.

    A verb this repo reaches through a `HANDLERS` guard must not sit bare in the package's
    `REMOTE_READONLY_VERBS`, and a verb the package guards must not sit in `TIER1`, whose
    contract is "read-only under ANY argument". The replay corpus cannot see a remote
    fail-open (4 of 1058 prompted records touch ssh), so this assertion is the evidence.
    Placement only: a verb guarded on BOTH sides with different guards is #1898's move.
    """
    spec = importlib.util.spec_from_file_location(
        "aar_1982", HOOKS / "auto-approve-readonly.py"
    )
    assert spec and spec.loader
    aar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aar)
    from claude_guard.checks.remote import REMOTE_GUARDED_VERBS

    # Non-vacuity: the guarded set the package exports must still carry the three regex
    # guards that sit on bare-listed verbs, so an empty result below cannot come from a
    # renamed or emptied export.
    assert {
        "journalctl",
        "rg",
        "sensors",
    } <= REMOTE_GUARDED_VERBS & _tables.REMOTE_READONLY_VERBS
    assert (
        boundary_violations(
            aar.HANDLERS,
            _readonly_tables.TIER1,
            _tables.REMOTE_READONLY_VERBS,
            REMOTE_GUARDED_VERBS,
        )
        == []
    )


# --- #1898: a guard that exists on both sides of the boundary reaches the same verdict ----------
# The package's `checks/remote_guards.py` is a copy of this repo's `HANDLERS` guards for the
# verbs both reach (git, sed, awk, find, sort, uniq, apt, dpkg, crontab, pipx, ...): this
# side keeps its copy for LOCAL commands, the package holds the one that judges an ssh
# stage. Nothing but this replay keeps the two copies agreeing. The vectors are the local
# tables this suite already maintains, filtered to the shared verbs.


def shared_guard_disagreements(argv_readonly, remote_argv_readonly, shared, vectors):
    """Vectors (argv lists) on a shared guarded verb where the two sides disagree."""
    bad = []
    for argv in vectors:
        if not argv or argv[0].rsplit("/", 1)[-1] not in shared:
            continue
        if bool(argv_readonly(argv)) != remote_argv_readonly(argv):
            bad.append(argv)
    return bad


def test_shared_guard_check_is_flagged_when_the_sides_disagree():
    def local(argv):
        return "git" if argv == ["git", "status"] else None

    def remote(argv):
        return argv == ["git", "push"]

    bad = shared_guard_disagreements(
        local, remote, {"git"}, [["git", "status"], ["git", "push"], ["ls"]]
    )
    assert bad == [["git", "status"], ["git", "push"]]


def test_shared_guard_check_is_clean_when_the_sides_agree():
    def both(argv):
        return argv == ["git", "status"]

    assert (
        shared_guard_disagreements(both, both, {"git"}, [["git", "status"], ["ls"]])
        == []
    )


@pytest.mark.skipif(
    not _CLAUDE_GUARD_DIR.is_dir(),
    reason="the deployed claude_guard package is not present, so there is no copy to agree with",
)
def test_every_guard_carried_on_both_sides_reaches_the_same_verdict():
    import shlex

    from claude_guard.checks.remote import remote_argv_readonly
    from claude_guard.checks.remote_guards import GUARDS

    import test_auto_approve_readonly as suite

    spec = importlib.util.spec_from_file_location(
        "aar_1898", HOOKS / "auto-approve-readonly.py"
    )
    assert spec and spec.loader
    aar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aar)

    shared = set(aar.HANDLERS) & set(GUARDS)
    # Non-vacuity: the port carried these thirteen, so an empty intersection is a rename.
    assert {
        "git",
        "sed",
        "awk",
        "find",
        "sort",
        "uniq",
        "apt",
        "dpkg",
        "crontab",
        "pipx",
    } <= shared
    vectors = []
    for command, _label in suite.APPROVE_LOCAL + suite.REJECT_LOCAL:
        if any(ch in command for ch in "|;&$`()<>\n"):
            continue  # a single argv only; the shapes above are classify()'s, not a guard's
        try:
            vectors.append(shlex.split(command))
        except ValueError:
            continue
    on_shared = [v for v in vectors if v and v[0] in shared]
    assert len(on_shared) >= 40, on_shared
    assert (
        shared_guard_disagreements(
            aar._argv_readonly, remote_argv_readonly, shared, vectors
        )
        == []
    )
