"""`scripts/validate/alert_rules.py`'s Python phase, with the tools faked.

The pytest shards install no promtool or amtool, so every tool call here goes through a fake
runner. What the tools themselves reject is proven by CI's `alert_rules.py --prove-red` step,
which runs the real binaries against the fixtures under `fixtures/alert_rules/`.

Run: uv run pytest scripts/validate/tests/test_validate_alert_rules.py
"""

import re

from lib.repo_paths import REPO
from validate import alert_rules


def _proof_runner(bad_code: int, bad_output: str, good_code: int = 0):
    """A runner that answers each proof's good fixture with `good_code` and its bad one with
    `bad_code` and `bad_output`."""

    def run(argv: list[str]) -> tuple[int, str]:
        if any(argv[-1].endswith(p.bad) for p in alert_rules.PROOFS):
            return bad_code, bad_output
        return good_code, ""

    return run


def test_the_render_produces_every_floor_file_is_clean():
    found = alert_rules.rendered_files()
    # Non-vacuity: the floor names a concrete file, so an emptied FLOOR fails here.
    assert "prometheus.yml" in alert_rules.FLOOR
    assert "scrape_configs:" in found["prometheus.yml"]
    assert alert_rules.floor_problems(found) == []


def test_a_render_missing_a_floor_file_is_flagged():
    problems = alert_rules.floor_problems({})
    assert problems and "prometheus.yml" in problems[0], problems


def test_every_discovered_input_gets_its_tool_check(tmp_path):
    rules = tmp_path / "rules"
    tests = tmp_path / "tests"
    rules.mkdir()
    tests.mkdir()
    (rules / "meta.yaml").write_text("groups: []\n")
    (tests / "meta.test.yaml").write_text("tests: []\n")
    config = tmp_path / "prometheus.yml"

    checks = alert_rules.tool_checks({"prometheus.yml": config}, rules, tests)

    assert checks == [
        ["promtool", "check", "config", "--syntax-only", str(config)],
        ["promtool", "check", "rules", str(rules / "meta.yaml")],
        ["promtool", "test", "rules", str(tests / "meta.test.yaml")],
    ]


def test_the_tree_with_tools_that_pass_is_clean():
    assert alert_rules.check_tree(run=lambda argv: (0, "")) == []


def test_a_tool_rejecting_the_rendered_config_is_flagged():
    problems = alert_rules.check_tree(run=lambda argv: (1, "FAILED: bad field"))
    config = [p for p in problems if "check config --syntax-only" in p]
    assert len(config) == 1, problems
    assert "FAILED: bad field" in config[0]


def test_tools_that_accept_good_and_reject_bad_fixtures_are_clean():
    markers = "\n".join(p.marker for p in alert_rules.PROOFS)
    assert alert_rules.prove_red(run=_proof_runner(1, markers)) == []


def test_a_tool_that_accepts_its_bad_fixture_is_flagged():
    problems = alert_rules.prove_red(run=_proof_runner(0, ""))
    assert len(problems) == len(alert_rules.PROOFS), problems
    assert all("which it must reject" in p for p in problems), problems


def test_a_tool_that_never_ran_is_flagged():
    """A missing binary also exits non-zero. Read as a rejection, it would pass the proof."""
    problems = alert_rules.prove_red(
        run=_proof_runner(127, "No such file or directory", good_code=127)
    )
    assert len(problems) == 2 * len(alert_rules.PROOFS), problems
    assert any("which it must accept" in p for p in problems), problems
    assert any("without saying" in p for p in problems), problems


def test_every_proof_names_fixtures_that_exist():
    """Non-vacuity: one proof per tool command, each with both fixtures committed."""
    commands = {(p.tool, *p.args) for p in alert_rules.PROOFS}
    assert commands == {
        ("promtool", "check", "rules"),
        ("promtool", "test", "rules"),
        ("amtool", "check-config"),
        ("promtool", "check", "config", "--syntax-only"),
    }
    for proof in alert_rules.PROOFS:
        assert (alert_rules.FIXTURES / proof.good).is_file(), proof.good
        assert (alert_rules.FIXTURES / proof.bad).is_file(), proof.bad


def test_ci_provisions_the_tools_then_runs_the_red_proof():
    """The red proof exists only as this CI step; dropping it leaves the tools unproven."""
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    provision = workflow.find("run: scripts/validate/alert_tools.sh\n")
    prove = workflow.find(
        "run: uv run python scripts/validate/alert_rules.py --prove-red\n"
    )
    hooks = workflow.find("- name: Run hooks")
    assert -1 < provision < prove < hooks, (provision, prove, hooks)
    assert re.search(
        r'entry = "uv run --frozen python scripts/validate/alert_rules\.py"',
        (REPO / "prek.toml").read_text(),
    )
