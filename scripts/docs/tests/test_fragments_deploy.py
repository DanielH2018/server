"""Tests for the deploy, landing and issue-claiming fragments.

Each renderer is checked against literals it is handed, and each reader against the real tree
for a member it must find, so a rename that empties a census fails by name.
"""

from pathlib import Path

import pytest

from docs.fragments_lib import fragments_deploy as fd
from lib.repo_paths import REPO


# --- land.sh verdicts and exit codes -------------------------------------------------------


def test_land_verdicts_renders_the_codes_and_vocabularies_it_is_given():
    out = fd.render_land_verdicts(
        [(0, "LAND_SETTLED", "done."), (75, "LAND_GAVE_UP", "gave up | tried.")],
        ["settled", "ci-red"],
        ["tag-miss"],
    )
    assert "| 0 | `LAND_SETTLED` | done. |" in out
    assert "| 75 | `LAND_GAVE_UP` | gave up \\| tried. |" in out
    assert "one of 2 words: `settled`, `ci-red`." in out
    assert "one of 1 causes in the landing annotation: `tag-miss`." in out


def test_enum_values_finds_the_verdicts_and_causes_land_prints():
    outcome = REPO / fd.LAND_OUTCOME
    assert {"settled", "deploy-failed", "unbuilt"} <= set(
        fd.enum_values(outcome, "Verdict")
    )
    assert {"soak-failed", "tag-miss", "promote-timeout"} <= set(
        fd.enum_values(outcome, "Cause")
    )


def test_enum_values_rejects_a_class_the_module_does_not_define():
    with pytest.raises(KeyError):
        fd.enum_values(REPO / fd.LAND_OUTCOME, "NoSuchEnum")


def test_land_exit_codes_include_every_code_land_sh_can_exit_with():
    codes = {value: name for value, name, _ in fd.land_exit_codes()}
    assert codes == {
        0: "LAND_SETTLED",
        1: "LAND_FAILED",
        64: "LAND_BAD_ARGS",
        75: "LAND_GAVE_UP",
    }


# --- the git-tree lock holders -------------------------------------------------------------


def _lock_tree(root: Path, *, unit_exec: str) -> Path:
    """A minimal tree holding one unit template."""
    unit_dir = root / fd.HOST_COMPONENTS / "ticker" / "units"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tick.service.j2").write_text(unit_exec)
    return root


_UNIT_WITH_LOCK = (
    "ExecStart=/usr/bin/flock -w 180 {{ server_git_tree_lock }} /bin/run\n"
)


def test_tree_lock_holders_lists_a_unit_that_wraps_flock(tmp_path):
    root = _lock_tree(tmp_path, unit_exec=_UNIT_WITH_LOCK)
    holders = fd.tree_lock_holders(root)
    assert [h["job"] for h in holders] == ["tick.service"]
    assert holders[0]["source"] == "hosts/components/ticker/units/tick.service.j2"


def test_tree_lock_holders_skips_a_unit_that_does_not_wrap_flock(tmp_path):
    root = _lock_tree(
        tmp_path, unit_exec="Environment=LOCK={{ server_git_tree_lock }}\n"
    )
    assert fd.tree_lock_holders(root) == []


def test_tree_lock_holders_in_the_real_tree_include_every_known_job():
    jobs = {h["job"] for h in fd.tree_lock_holders()}
    assert "homelab-checkout-sync.service" in jobs
