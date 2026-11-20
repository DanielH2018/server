"""CI's pytest legs run a pull request's selection, and every other event's whole census.

`scripts/dev/pytest_shard.py --changed-since` picks the modules and
`scripts/dev/tests/test_pytest_shard_selection.py` covers it. This module holds the workflow
to the four things the script cannot see: which events hand it a base, that the matrix
starts the legs `pytest_plan` planned, that a leg with an empty list skips every later step rather
than running pytest on nothing, and that the gate can read whether the legs ran the whole
census.

Run: uv run pytest tests/repo/test_ci_pytest_selects_affected_modules.py
"""

import pytest

from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SELECT = "Select this shard's test files"
EMPTY_LEG_GUARD = "steps.select.outputs.count != '0'"
# The census names members it must find: a renamed step would otherwise leave the check.
GUARDED_STEPS = frozenset(
    {
        "Install the tools the tests run",
        "Restore pytest's rewritten test modules",
        "Run the test suite",
        "Save pytest's rewritten test modules",
        "Check every heavy module's recorded weight",
    }
)


def _job() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest"]


def _unguarded_after_select(steps: list[dict]) -> list[str]:
    """The steps after the select step that would still run on a leg with an empty list."""
    names = [step.get("name", step.get("uses", "")) for step in steps]
    after = steps[names.index(SELECT) + 1 :]
    return [
        step.get("name", step.get("uses", ""))
        for step in after
        if EMPTY_LEG_GUARD not in str(step.get("if", ""))
    ]


def _run_step(tmp_path, step: dict, tool: str, event: str, passed: dict) -> str:
    """What `step` hands `tool` on `event`, with `tool` replaced by an echo.

    The step's own `env:` names each variable, so a variable the workflow stops passing is
    unset here too.
    """
    stubs = fake_bin(tmp_path / "bin", **{tool: 'echo "$@"\n'})
    done = run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        stub_bin=stubs,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_EVENT_NAME": event,
            "RUNNER_TEMP": str(tmp_path),
            **{name: passed[value] for name, value in step["env"].items()},
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _arguments(tmp_path, event: str, green_base: str = "") -> str:
    """What the select step hands `uv` on `event`."""
    step = next(s for s in _job()["steps"] if s.get("name") == SELECT)
    passed = {
        "${{ matrix.shard }}": "2",
        "${{ needs.prove.outputs.base }}": green_base,
    }
    return _run_step(tmp_path, step, "uv", event, passed)


def _plan_step() -> dict:
    plan = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest_plan"]
    return next(s for s in plan["steps"] if s.get("id") == "plan")


def test_a_pull_request_diffs_against_the_commit_it_was_merged_onto(tmp_path):
    arguments = _arguments(tmp_path, "pull_request")

    assert "--changed-since HEAD^1" in arguments
    assert "--shard 2" in arguments


def test_a_push_diffs_against_the_green_base_the_prove_job_named(tmp_path):
    arguments = _arguments(tmp_path, "push", green_base="f" * 40)

    assert f"--changed-since {'f' * 40}" in arguments


@pytest.mark.parametrize(
    ("event", "green_base"),
    [("push", ""), ("workflow_dispatch", "")],
    ids=["push-with-no-green-ancestor", "dispatch"],
)
def test_a_push_with_no_base_and_a_dispatch_hand_the_split_no_base(
    tmp_path, event, green_base
):
    arguments = _arguments(tmp_path, event, green_base)

    assert "pytest_shard.py" in arguments and "--changed-since" not in arguments


def test_the_matrix_starts_the_planned_legs_and_every_leg_without_a_plan():
    axis = " ".join(_job()["strategy"]["matrix"]["shard"].split())

    assert axis.startswith("${{ fromJSON(needs.pytest_plan.outputs.legs || '[")
    assert "pytest_plan" in _job()["needs"]


@pytest.mark.parametrize(
    ("event", "green_base", "since"),
    [("pull_request", "", "HEAD^1"), ("push", "f" * 40, "f" * 40)],
    ids=["pull-request", "push"],
)
def test_the_plan_diffs_against_the_base_its_legs_select_against(
    tmp_path, event, green_base, since
):
    passed = {"scripts": "scripts", "${{ needs.prove.outputs.base }}": green_base}
    arguments = _run_step(tmp_path, _plan_step(), "python3", event, passed)

    assert arguments.split() == [
        "scripts/dev/pytest_shard.py",
        "--plan",
        "--changed-since",
        since,
    ]


@pytest.mark.parametrize(
    ("event", "green_base", "plans"),
    [
        ("pull_request", "", True),
        ("push", "f" * 40, True),
        # The legs run the whole census, so every leg is needed.
        ("push", "", False),
        ("workflow_dispatch", "", False),
    ],
    ids=["pull-request", "push-with-a-base", "push-with-no-base", "dispatch"],
)
def test_only_a_run_with_a_base_plans(event, green_base, plans):
    condition = (
        " ".join(_plan_step()["if"].split())
        .replace("github.event_name", repr(event))
        .replace("needs.prove.outputs.base", repr(green_base))
        .replace("||", " or ")
    )
    # The input is ci.yml's own condition, with every name substituted by a literal.
    assert eval(condition) is plans


def test_the_plan_job_publishes_the_plan_steps_legs():
    plan = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest_plan"]

    assert plan["outputs"] == {"legs": "${{ steps.plan.outputs.legs }}"}


def test_every_step_after_the_selection_skips_on_an_empty_leg():
    steps = _job()["steps"]
    names = {step.get("name") for step in steps}

    assert GUARDED_STEPS <= names, sorted(GUARDED_STEPS - names)
    assert _unguarded_after_select(steps) == []


def test_a_step_that_would_run_on_an_empty_leg_is_flagged():
    steps = [
        {"name": SELECT, "id": "select", "run": "true"},
        {"name": "Run the test suite", "run": "pytest"},
        {
            "name": "Save",
            "if": f"{EMPTY_LEG_GUARD} && github.ref == 'refs/heads/master'",
        },
    ]

    assert _unguarded_after_select(steps) == ["Run the test suite"]


def test_the_job_publishes_whether_its_legs_ran_the_whole_census():
    job = _job()
    select = next(s for s in job["steps"] if s.get("name") == SELECT)

    assert select["id"] == "select"
    assert job["outputs"] == {"scope": "${{ steps.select.outputs.scope }}"}
