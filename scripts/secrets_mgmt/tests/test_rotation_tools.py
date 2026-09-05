"""Tests for the injectable boundaries in scripts/secrets_mgmt/rotation_tools.py.

These sat in test_secret_rotation.py while the functions did. They cover the seam module's
own contracts — the registry file format, the decrypt arm's three outcomes, and the cadence
table it holds — so they moved with the code rather than staying beside the caller.

Run: uv run pytest scripts/secrets_mgmt/tests/test_rotation_tools.py
"""

import subprocess

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


# ── the decrypt arm: three outcomes, and only one of them is a dict ──────────
# Synthetic values throughout: CI has no age key, so a test may never touch the real
# encrypted store.
def test_decrypt_without_an_age_key_returns_none():
    """CI has no age key.

    The arm must go quiet there, not raise — `audit --check` is a prek/CI gate over this very file,
    so a hard failure would fail every secrets PR.
    """
    tools, recorded = build_tools(
        Fakes(run_error=subprocess.CalledProcessError(1, "sops", stderr="no key"))
    )
    assert tools.sops_decrypt("whatever.yml") is None
    assert process_calls(recorded)[0][0][:2] == ["sops", "--decrypt"]


def test_a_nonzero_exit_is_a_failed_decrypt_not_an_empty_one():
    """The fake runner honours `check=True`, or the failure path stops being reachable.

    A runner that returned the CompletedProcess anyway would hand `decrypted_values` an empty
    stdout, and `{}` — "decrypted fine, no secrets" — reads identically to a clean store while
    meaning the opposite of None.
    """
    tools, _ = build_tools(Fakes(run_rc=1))
    assert tools.sops_decrypt("whatever.yml") is None


def test_decrypt_drops_the_sops_metadata_key():
    tools, _ = build_tools(Fakes(run_stdout="a_push_token: abc\nsops:\n  mac: xyz\n"))
    assert tools.sops_decrypt("whatever.yml") == {"a_push_token": "abc"}
