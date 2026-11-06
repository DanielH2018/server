"""Every `gh` call and GHCR login in the `promote` workflow runs inside its `retry` helper.

One transient 502, 503 or EOF on such a call failed three promote runs between 2026-10-30 and
2026-11-06. `scripts/deploy_tools/tests/test_promote_settle_retry.py` runs the helper itself.

Run: uv run pytest ansible/tests/repo/test_promote_workflow_retry.py
"""

import copy
import re

import pytest
from test_promote_workflow import WORKFLOW, _load, _steps

UNRETRIED = re.compile(r"(?<!retry )(?<![\w./-])gh [a-z]+|docker login")
RETRY_CALLERS = re.compile(r"(?<![\w-])(?:retry|ghcr_login)\b")
LOAD_RETRY = 'eval "$RETRY_FUNCTIONS"'
RECORD = "Record prod's digest"
APPLY = "Apply the verdict"
DISPATCH = "Dispatch a run for a newer green commit"
LOGIN = "Log in to GHCR read-only"
# The census must find each of these, so a renamed step cannot empty it.
RETRIED = {
    ("tag", RECORD): "retry gh variable set PROD_DIGEST",
    ("settle", APPLY): "retry gh variable set PROD_DIGEST",
    ("reset-move", RECORD): "retry gh variable set PROD_DIGEST",
    ("settle", DISPATCH): "retry gh workflow run",
    ("promote", LOGIN): "ghcr_login",
}


def retry_problems(wf: dict) -> list[str]:
    """Every call outside the helper, and every step that calls it without loading it."""
    found = []
    functions = str((wf.get("env") or {}).get("RETRY_FUNCTIONS", ""))
    if "retry()" not in functions or "docker login" not in functions:
        found.append("the workflow env's RETRY_FUNCTIONS lacks retry or the GHCR login")
    # A list, because unnamed steps and steps sharing a name would collide as dict keys.
    runs = [
        (job_name, step.get("name") or step.get("id"), str(step.get("run", "")))
        for job_name, job in (wf.get("jobs") or {}).items()
        for step in _steps(job)
    ]
    for job_name, step_name, script in runs:
        code = [
            line for line in script.splitlines() if not line.lstrip().startswith("#")
        ]
        for line in code:
            if match := UNRETRIED.search(line):
                found.append(f"{job_name}: {step_name}: runs {match[0]} outside retry")
        if LOAD_RETRY not in script and RETRY_CALLERS.search("\n".join(code)):
            found.append(f"{job_name}: {step_name}: calls retry without {LOAD_RETRY}")
    for (job_name, step_name), call in RETRIED.items():
        if not any(
            (job, name) == (job_name, step_name) and call in script
            for job, name, script in runs
        ):
            found.append(f"{job_name}: {step_name}: does not run {call}")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


def test_every_call_runs_inside_retry_is_clean(workflow):
    assert retry_problems(workflow) == []


RED = {
    "tag's PROD_DIGEST write": ("tag", RECORD, "retry gh", "gh", "runs gh variable"),
    "settle's PROD_DIGEST write": (
        "settle",
        APPLY,
        "retry gh variable set PROD_DIGEST",
        "gh variable set PROD_DIGEST",
        "runs gh variable",
    ),
    "reset-move's PROD_DIGEST write": (
        "reset-move",
        RECORD,
        "retry gh",
        "gh",
        "runs gh variable",
    ),
    "the dispatch": (
        "settle",
        DISPATCH,
        "retry gh workflow run",
        "gh workflow run",
        "runs gh workflow",
    ),
    "the run list read": ("settle", DISPATCH, "retry gh api", "gh api", "runs gh api"),
    "the GHCR login": (
        "promote",
        LOGIN,
        "ghcr_login",
        "docker login ghcr.io",
        "runs docker login",
    ),
    "a step that never loads retry": ("settle", APPLY, LOAD_RETRY, "", "without eval"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_a_call_outside_retry_is_flagged(workflow, case):
    job, step_name, old, new, expected = RED[case]
    broken = copy.deepcopy(workflow)
    step = next(s for s in broken["jobs"][job]["steps"] if s.get("name") == step_name)
    step["run"] = step["run"].replace(old, new)
    assert (
        step["run"]
        != next(
            s for s in workflow["jobs"][job]["steps"] if s.get("name") == step_name
        )["run"]
    ), f"{case}: the mutation left the step unchanged"
    assert any(
        line.startswith(f"{job}: {step_name}: ") and expected in line
        for line in retry_problems(broken)
    ), retry_problems(broken)


BARE = "gh variable set PROD_DIGEST --body x"


@pytest.mark.parametrize(
    "added",
    [
        [{"run": BARE}, {"uses": "actions/checkout@v4"}],
        [{"name": "Same", "run": BARE}, {"name": "Same", "run": "true"}],
    ],
    ids=["unnamed-step-before-a-uses-step", "step-sharing-a-later-step-s-name"],
)
def test_a_bare_call_in_a_step_with_a_colliding_name_is_flagged(workflow, added):
    broken = copy.deepcopy(workflow)
    broken["jobs"]["tag"]["steps"] += added
    assert any(
        line.startswith("tag: ") and "runs gh variable outside retry" in line
        for line in retry_problems(broken)
    ), retry_problems(broken)
