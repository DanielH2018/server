"""Tests for the deploy, landing and issue-claiming fragments.

Each renderer is checked against literals it is handed, and each reader against the real tree
for a member it must find, so a rename that empties a census fails by name.
"""

from pathlib import Path

import pytest

from fragments_lib import fragments_deploy as fd
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


def _lock_tree(root: Path, *, cron_body: str, unit_exec: str) -> Path:
    """A minimal tree: one cron template and one unit template."""
    templates = root / fd.HOST_COMPONENTS / "nightly" / "templates"
    templates.mkdir(parents=True)
    (templates / "nightly.sh.j2").write_text(cron_body)
    (templates / "bystander.sh.j2").write_text("echo no lock here\n")
    unit_dir = root / fd.HOST_COMPONENTS / "ticker" / "units"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tick.service.j2").write_text(unit_exec)
    return root


_CRON_WITH_LOCK = (
    "{% from 'git-tree-lock.j2' import take_git_tree_lock with context %}\n"
    "{{ take_git_tree_lock('nightly') }}\n"
)
_UNIT_WITH_LOCK = (
    "ExecStart=/usr/bin/flock -w 180 {{ server_git_tree_lock }} /bin/run\n"
)


def test_tree_lock_holders_lists_a_cron_and_a_unit_and_skips_a_bystander(
    tmp_path,
):
    root = _lock_tree(tmp_path, cron_body=_CRON_WITH_LOCK, unit_exec=_UNIT_WITH_LOCK)
    assert [h["job"] for h in fd.tree_lock_holders(root)] == [
        "tick.service",
        "nightly",
    ]


def test_tree_lock_holders_finds_a_cron_script_shipped_in_a_host_component(tmp_path):
    root = _lock_tree(tmp_path, cron_body="echo none\n", unit_exec=_UNIT_WITH_LOCK)
    component = root / fd.HOST_COMPONENTS / "nightly-sweep" / "templates"
    component.mkdir(parents=True)
    (component / "sweep.sh.j2").write_text(_CRON_WITH_LOCK.replace("nightly", "sweep"))
    holder = next(h for h in fd.tree_lock_holders(root) if h["job"] == "sweep")
    assert holder["source"] == "hosts/components/nightly-sweep/templates/sweep.sh.j2"


def test_tree_lock_holders_skips_a_unit_that_does_not_wrap_flock(tmp_path):
    root = _lock_tree(
        tmp_path,
        cron_body=_CRON_WITH_LOCK,
        unit_exec="Environment=LOCK={{ server_git_tree_lock }}\n",
    )
    assert "tick.service" not in [h["job"] for h in fd.tree_lock_holders(root)]


def test_tree_lock_holders_rejects_a_template_that_imports_the_macro_and_never_calls_it(
    tmp_path,
):
    root = _lock_tree(
        tmp_path,
        cron_body="{% from 'git-tree-lock.j2' import take_git_tree_lock with context %}\n",
        unit_exec=_UNIT_WITH_LOCK,
    )
    with pytest.raises(ValueError, match="never calls it"):
        fd.tree_lock_holders(root)


def test_tree_lock_holders_in_the_real_tree_include_every_known_job():
    jobs = {h["job"] for h in fd.tree_lock_holders()}
    assert {
        "homelab-checkout-sync.service",
        "docs-refresh",
        "eval-run",
    } <= jobs
