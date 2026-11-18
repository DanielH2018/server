"""The nightly sweep runs every test module, and its result can never grade a commit.

ci.yml's pytest legs run a pull request's selection, so `pytest-sweep.yml` is the run that
finds a module the selection table left out. This module holds the workflow to what makes it
that run, and to what keeps a red sweep from reading as a red master commit.

Run: uv run pytest tests/repo/test_pytest_sweep_workflow.py
"""

import copy

import pytest
from _helpers import REPO
from ci_verdict import CI_GATE_NAME
from lib import yaml_fast

SWEEP = REPO / ".github" / "workflows" / "pytest-sweep.yml"
RUN_STEP = "Run the whole test suite"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(SWEEP.read_text(encoding="utf-8"))


def problems(workflow: dict) -> list[str]:
    found = []
    triggers = workflow[True]  # PyYAML parses the `on:` key as True
    if not triggers.get("schedule"):
        found.append("the sweep has no schedule")
    if {"push", "pull_request"} & set(triggers):
        found.append("the sweep runs per commit, where ci.yml's selection already runs")
    group = str((workflow.get("concurrency") or {}).get("group", ""))
    if "github.workflow" not in group or "github.sha" in group:
        found.append(
            f"concurrency group {group!r} can join ci.yml's group for a commit"
        )
    for job_id, job in workflow["jobs"].items():
        if job.get("name") == CI_GATE_NAME:
            found.append(f"job {job_id} reports under the name of ci.yml's gate")
        writes = (job.get("permissions") or {}).get("issues") == "write"
        if writes and any("pytest" in str(s.get("run", "")) for s in job["steps"]):
            found.append(f"job {job_id} runs the suite with a token that writes issues")
    steps = workflow["jobs"]["sweep"]["steps"]
    suite = next((s for s in steps if s.get("name") == RUN_STEP), None)
    if suite is None:
        found.append(f"the sweep job has no {RUN_STEP!r} step")
    elif "--changed-since" in suite["run"] or "pytest_shard" in suite["run"]:
        found.append("the sweep hands pytest a selection or a shard")
    if not any(str(s.get("uses", "")).endswith("/pytest-tools") for s in steps):
        found.append("the sweep does not install the tools ci.yml's legs install")
    return found


def test_the_sweep_workflow_is_clean(workflow):
    assert problems(workflow) == []


def test_a_job_under_the_gates_name_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    broken["jobs"]["sweep"]["name"] = CI_GATE_NAME
    assert problems(broken) == ["job sweep reports under the name of ci.yml's gate"]


def test_a_sweep_that_runs_a_selection_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    suite = next(
        s for s in broken["jobs"]["sweep"]["steps"] if s.get("name") == RUN_STEP
    )
    suite["run"] += "uv run python scripts/dev/pytest_shard.py --changed-since HEAD^1\n"
    assert problems(broken) == ["the sweep hands pytest a selection or a shard"]


def test_only_a_run_on_master_reports(workflow):
    report = workflow["jobs"]["report"]
    assert "github.ref == 'refs/heads/master'" in str(report["if"])
    assert "always()" in str(report["if"]), "a red sweep must still reach the report"
