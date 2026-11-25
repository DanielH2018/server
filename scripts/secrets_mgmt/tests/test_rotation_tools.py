"""Tests for the injectable boundaries in scripts/secrets_mgmt/rotation_tools.py.

These sat in test_secret_rotation.py while the functions did. They cover the seam module's
own contracts — the registry file format and the cadence table it holds — so they moved with
the code rather than staying beside the caller. The `sops_set` tests cover `sops_io`, the leaf
that holds the one write to a SOPS file.

Run: uv run pytest scripts/secrets_mgmt/tests/test_rotation_tools.py
"""

import datetime as dt
from pathlib import Path

import pytest

from secrets_mgmt import secret_rotation as sr
from secrets_mgmt import sops_io
from secrets_mgmt.rotation_tools import (
    REGISTRY_FILE,
    RotationTools,
    load_registry,
    save_registry,
)
from secrets_mgmt.secrets_lib.registry_model import Entry, Registry, RegistryError


# A save/load corruption is SILENT, so pin the contract: lossless round trip, MANAGED header kept,
# keys sorted for a stable diff.
def test_registry_round_trips_losslessly(tmp_path):
    reg = Registry(
        {
            "b_token": Entry("external", dt.date(2026, 6, 1), "record"),
            "a_token": Entry("assisted", dt.date(2026, 5, 15)),
            "c_user": Entry("ignore", None),
        }
    )
    path = str(tmp_path / "reg.yml")
    save_registry(reg, path)
    assert load_registry(path) == reg


def test_the_committed_registry_saves_back_byte_for_byte(tmp_path):
    """A load and save of the live file changes nothing, so `sync` and `record` diff cleanly."""
    path = tmp_path / "reg.yml"
    save_registry(load_registry(REGISTRY_FILE), str(path))
    assert path.read_text() == Path(REGISTRY_FILE).read_text()


def test_saved_registry_keeps_managed_header_and_sorts_keys(tmp_path):
    path = str(tmp_path / "reg.yml")
    save_registry(Registry({"z_tok": Entry("pinned"), "a_tok": Entry("pinned")}), path)
    text = (tmp_path / "reg.yml").read_text()
    assert text.startswith("# Secret rotation registry — MANAGED")
    assert text.index("\n  a_tok:") < text.index("\n  z_tok:")  # sort_keys=True


def test_load_registry_missing_file_returns_an_empty_registry(tmp_path):
    missing = str(tmp_path / "does-not-exist.yml")
    assert load_registry(missing) == Registry()


def test_load_parses_dates_and_defaults_a_missing_tier(tmp_path):
    path = tmp_path / "reg.yml"
    path.write_text("entries:\n  tok: {last_rotated: '2026-03-13'}\n")
    assert load_registry(str(path)).entries["tok"] == Entry(
        "assisted", dt.date(2026, 3, 13)
    )


@pytest.mark.parametrize(
    ("row", "named"),
    [
        ("{tier: assisted, last_rotated: '2026-13-01'}", "last_rotated"),
        ("{tier: assisted, last_rotated: 'soon'}", "last_rotated"),
        ("{tier: assisted, rotated: '2026-01-01'}", "unknown field(s) rotated"),
        ("{tier: 365}", "tier"),
        ("assisted", "a row is a mapping"),
    ],
)
def test_a_malformed_row_fails_at_load_naming_the_row(tmp_path, row, named):
    path = tmp_path / "reg.yml"
    path.write_text(f"entries:\n  bad_tok: {row}\n")
    with pytest.raises(RegistryError, match=r"^bad_tok: .*") as raised:
        load_registry(str(path))
    assert named in str(raised.value)


def test_the_default_tier_table_is_the_one_secret_rotation_assigns():
    """The cadence table is written out twice, and this holds the two VALUES equal.

    `secret_rotation.TIER_DAYS` has to stay a literal there (gen_doc_fragments AST-reads it)
    and `rotation_tools` may not import its own facade to reach it, so each file spells the
    table out. Nothing else compares them: editing one cadence and not the other would ship a
    tool whose audit and whose published page disagree.

    It does NOT guard the literal FORM the fragment reader needs — an equal value spelled
    `dict(DEFAULT_TIER_DAYS)` would pass here and break `gen_doc_fragments.py`, whose
    `module_constant` runs `ast.literal_eval` over the assignment rather than importing the
    module. The guard for that half is
    `test_every_committed_fragment_matches_what_the_generator_writes_now`, which runs the
    generator.
    """
    assert RotationTools().tier_days == sr.TIER_DAYS


# ── sops_io.sops_set: the one write to a SOPS file ─────────────────────────
def test_sops_set_bounds_its_subprocess_with_a_timeout():
    """A `sops` that never returns must not hang `cluster_vars.py write`, its caller.

    `subprocess.run` waits forever without this kwarg, so the bound is asserted where it is
    handed to the process rather than where it is written.
    """
    calls = []
    sops_io.sops_set("a_push_token", "deadbeef", run=lambda cmd, **kw: calls.append(kw))
    assert [kw.get("timeout") for kw in calls] == [30]


def test_sops_set_sends_the_value_on_stdin_never_in_argv():
    """argv is world-readable in /proc/<pid>/cmdline, so the value travels on stdin."""
    calls = []
    sops_io.sops_set(
        "a_push_token", "deadbeef", run=lambda cmd, **kw: calls.append((cmd, kw))
    )
    ((cmd, kw),) = calls
    assert not any("deadbeef" in part for part in cmd)
    assert kw["input"] == '"deadbeef"'


def test_sops_set_writes_the_file_it_is_given_and_secrets_yml_by_default():
    """`cluster_vars.py write` sets keys in the cluster file through the same argv builder."""
    argvs = []
    sops_io.sops_set("a_key", "v", run=lambda cmd, **_kw: argvs.append(cmd))
    sops_io.sops_set(
        "a_key",
        "v",
        path="/elsewhere/cluster.env",
        run=lambda cmd, **_kw: argvs.append(cmd),
    )
    assert [cmd[3] for cmd in argvs] == [
        sops_io.SECRETS_FILE,
        "/elsewhere/cluster.env",
    ]
