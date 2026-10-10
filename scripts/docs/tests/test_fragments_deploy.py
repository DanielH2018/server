"""Tests for the deploy, landing and issue-claiming fragments.

Each renderer is checked against literals it is handed, and each reader against the real tree
for a member it must find, so a rename that empties a census fails by name.
"""

import argparse
from pathlib import Path

import pytest

import fragments_deploy as fd
from fragment_readers import module_constant
from dev.findings_lib import cli as findings_cli
from dev.findings_lib import issue_model
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
    assert {"settled", "deploy-failed", "tip-outran-retries"} <= set(
        fd.enum_values(outcome, "Verdict")
    )
    assert {"host-lookup", "tag-miss", "deploy-exit-other"} <= set(
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
    """A minimal tree: one cron template, one unit template and a deploy wrapper."""
    templates = root / fd.INITIAL_SETUP_TEMPLATES
    templates.mkdir(parents=True)
    (templates / "nightly.sh.j2").write_text(cron_body)
    (templates / "bystander.sh.j2").write_text("echo no lock here\n")
    unit_dir = root / fd.SETUP_ROLES / "deployer" / "templates"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tick.service.j2").write_text(unit_exec)
    wrapper = root / fd.DEPLOY_UNDER_LOCKS
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("lock = tree_lock_path()\n")
    return root


_CRON_WITH_LOCK = (
    "{% from 'git-tree-lock.j2' import take_git_tree_lock with context %}\n"
    "{{ take_git_tree_lock('nightly') }}\n"
)
_UNIT_WITH_LOCK = (
    "ExecStart=/usr/bin/flock -w 180 {{ server_git_tree_lock }} /bin/run\n"
)


def test_tree_lock_holders_lists_a_cron_unit_and_the_wrapper_and_skips_a_bystander(
    tmp_path,
):
    root = _lock_tree(tmp_path, cron_body=_CRON_WITH_LOCK, unit_exec=_UNIT_WITH_LOCK)
    assert [h["job"] for h in fd.tree_lock_holders(root)] == [
        "tick.service",
        "nightly",
        "deploy.sh",
    ]


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
        "gitops-deploy.service",
        "secret-rotate",
        "docs-refresh",
        "eval-run",
        "deploy.sh",
    } <= jobs


def _unnamed(holders: list[dict[str, str]], message: tuple[str, ...]) -> list[str]:
    text = " ".join(" ".join(line.split()) for line in message)
    return [h["job"] for h in holders if h["job"] not in text]


def test_a_holder_missing_from_the_runtime_message_is_flagged():
    holders = [{"job": "new-cron"}, {"job": "deploy.sh"}]
    message = ("A deploy is already running. Likely holders: another deploy.sh",)
    assert _unnamed(holders, message) == ["new-cron"]


def test_the_deploy_message_names_every_derived_tree_lock_holder():
    message = module_constant(REPO / fd.DEPLOY_UNDER_LOCKS, "TREE_LOCK_HOLDERS")
    assert _unnamed(fd.tree_lock_holders(), message) == []


# --- the findings CLI ----------------------------------------------------------------------


def test_findings_labels_render_one_row_per_label_with_its_colour():
    out = fd.render_findings_labels([("manual", "6c7086", "Reserved | for you")])
    assert "| `manual` | `#6c7086` | Reserved \\| for you |" in out


def test_findings_labels_match_the_labels_the_module_creates_at_runtime():
    # The domain labels are added by a loop, which only the loop's own AST can supply.
    read = {
        name: (colour, text)
        for name, colour, text in fd.findings_labels(REPO / fd.ISSUE_MODEL)
    }
    assert read == issue_model.LABELS
    assert {"manual", "claimed", "domain/cicd"} <= set(read)


def test_findings_labels_reject_a_module_without_the_domain_loop(tmp_path):
    module = tmp_path / "issue_model.py"
    module.write_text(
        'DOMAINS = ("a",)\nLABELS: dict[str, tuple[str, str]] = {"x": ("000000", "y")}\n'
    )
    with pytest.raises(KeyError, match="DOMAINS"):
        fd.findings_labels(module)


def test_findings_subcommands_match_the_ones_argparse_registers():
    read = [name for name, _ in fd.findings_subcommands(REPO / fd.FINDINGS_LIB)]
    parser = findings_cli._parser("x")
    registered = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(read) == set(registered.choices)
    assert {"open", "defer", "claim", "reap", "next", "sync-labels"} <= set(read)


def test_findings_subcommands_are_found_in_any_module_of_the_package(tmp_path):
    (tmp_path / "elsewhere.py").write_text(
        'sub.add_parser("late", help="added in another module")\nsub.add_parser("bare")\n'
    )
    assert fd.findings_subcommands(tmp_path) == [
        ("late", "added in another module"),
        ("bare", ""),
    ]
