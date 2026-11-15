"""CI's pytest legs run a pull request's selection, and every other event's whole census.

`scripts/dev/pytest_shard.py --changed-since` picks the modules and
`scripts/dev/tests/test_pytest_shard_selection.py` covers it. This module holds the workflow
to the three things the script cannot see: which events hand it a base, that a leg with an
empty list skips every later step rather than running pytest on nothing, and that the gate
can read whether the legs ran the whole census.

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
        "Install sops, age and apt-utils for the tests",
        "Deploy the dotfiles packages the hooks and the pruner import",
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


def _arguments(tmp_path, event: str) -> str:
    """What the select step hands `uv` on `event`, with `uv` replaced by an echo."""
    step = next(s for s in _job()["steps"] if s.get("name") == SELECT)
    stubs = fake_bin(tmp_path / "bin", uv='echo "$@"\n')
    done = run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        stub_bin=stubs,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_EVENT_NAME": event,
            "SHARD": "2",
            "SHARDS": "6",
            "RUNNER_TEMP": str(tmp_path),
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_a_pull_request_diffs_against_the_commit_it_was_merged_onto(tmp_path):
    arguments = _arguments(tmp_path, "pull_request")

    assert "--changed-since HEAD^1" in arguments
    assert "--of 6 --shard 2" in arguments


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_a_push_and_a_dispatch_hand_the_split_no_base(tmp_path, event):
    arguments = _arguments(tmp_path, event)

    assert "pytest_shard.py" in arguments and "--changed-since" not in arguments


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
