"""Tests for the injectable boundaries in scripts/secrets_mgmt/rotation_tools.py.

These sat in test_secret_rotation.py while the functions did. They cover the seam module's
own contracts — the registry file format and the cadence table it holds — so they moved with
the code rather than staying beside the caller.

Run: uv run pytest scripts/secrets_mgmt/tests/test_rotation_tools.py
"""

from secrets_mgmt import rotation_tools
from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import Fakes, build_tools, process_calls
from secrets_mgmt.rotation_tools import RotationTools, load_registry, save_registry


# The registry is the single plaintext source of names/tiers/dates. A save/load
# corruption is SILENT (the next sync/audit reads garbage), so pin the contract:
# round-trips losslessly, keeps the MANAGED header, and sorts keys deterministically
# (sort_keys=True keeps the committed file diff-stable as secrets are added).
def test_registry_round_trips_losslessly(tmp_path):
    reg = {
        "entries": {
            "b_token": {"tier": "auto", "last_rotated": "2026-06-01"},
            "a_token": {"tier": "assisted", "last_rotated": "2026-05-15"},
        }
    }
    path = str(tmp_path / "reg.yml")
    save_registry(reg, path)
    assert load_registry(path) == reg


def test_saved_registry_keeps_managed_header_and_sorts_keys(tmp_path):
    path = str(tmp_path / "reg.yml")
    save_registry(
        {"entries": {"z_tok": {"tier": "auto"}, "a_tok": {"tier": "auto"}}}, path
    )
    text = (tmp_path / "reg.yml").read_text()
    assert text.startswith("# Secret rotation registry — MANAGED")
    assert text.index("\n  a_tok:") < text.index("\n  z_tok:")  # sort_keys=True


def test_load_registry_missing_file_returns_empty_skeleton(tmp_path):
    missing = str(tmp_path / "does-not-exist.yml")
    assert load_registry(missing) == {"entries": {}}


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


# ── the subprocess arm is time-bounded ──────────────────────────────────────
def test_sops_set_bounds_its_subprocess_with_a_timeout():
    """A `sops` that never returns must not hang the cron that called it.

    `sops_set` runs unattended from the weekly rotate cron, which holds the repo-tree lock the
    deploy pipeline shares while it runs. `subprocess.run` waits forever without this kwarg, so
    the bound is asserted where it is handed to the process rather than where it is written.
    """
    tools, recorded = build_tools(Fakes())
    tools.sops_set("a_push_token", "deadbeef")

    timeouts = [
        (cmd[1], kwargs.get("timeout")) for cmd, kwargs in process_calls(recorded)
    ]
    assert timeouts == [("set", 30)], (
        "every sops call must carry a timeout: %s" % timeouts
    )


def test_sops_set_writes_the_file_it_is_given_and_secrets_yml_by_default():
    """`cluster_vars.py write` sets keys in the cluster file through the same argv builder."""
    argvs = []
    rotation_tools.sops_set("a_key", "v", run=lambda cmd, **_kw: argvs.append(cmd))
    rotation_tools.sops_set(
        "a_key",
        "v",
        path="/elsewhere/cluster.env",
        run=lambda cmd, **_kw: argvs.append(cmd),
    )
    assert [cmd[3] for cmd in argvs] == [
        rotation_tools.SECRETS_FILE,
        "/elsewhere/cluster.env",
    ]
