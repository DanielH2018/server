"""The dispatch of a run for a newer green commit, which `settle` and `reconcile` share.

Both jobs check out nothing, so the dispatch is the workflow env's `dispatch_newer_green`
function, which each job's dispatch step loads and calls with the commit it settled: `settle`
after it applies T's verdict, `reconcile` after it moves `last-good` for a lost settle.
Each test runs a step's own `run:` under the fakes of `test_promote_settle.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_dispatch.py
"""

import copy

import pytest
from test_promote_settle import T, _functions, _run, _step
from test_promote_settle import jobs as jobs

# -- settle and reconcile: the dispatch of a run for a newer green commit -------------------

NEWER, OLDER = "b" * 40, "c" * 40
DISPATCH = (
    f"gh workflow run promote.yml -R DanielH2018/server --ref master -f sha={NEWER}"
)
DISPATCH_STEP = "Dispatch a run for a newer green commit"
# The job token is the one `actions: write` reaches, since `gh` prefers GH_TOKEN; the `sha` input
# sends the run through render's CI re-check.
DISPATCH_TOKEN = "${{ github.token }}"
DISPATCH_CALL = ('eval "$DISPATCH_FUNCTIONS"', "dispatch_newer_green ")
DISPATCH_ARGS = ("gh workflow run promote.yml", "--ref master", "-f sha=")
# Each job that dispatches, with the env var its step passes as the settled commit.
DISPATCHERS = {"settle": "TARGET", "reconcile": "COMMIT"}


def dispatch_problems(name: str, job: dict, functions: str) -> list[str]:
    """Every broken property of job `name`'s dispatch step and the function it runs."""
    step = next((s for s in job["steps"] if s.get("name") == DISPATCH_STEP), None)
    if step is None:
        return [f"{name}: has no step named {DISPATCH_STEP!r}"]
    found = []
    if (step.get("env") or {}).get("GH_TOKEN") != DISPATCH_TOKEN:
        found.append(f"{name}: the dispatch step's GH_TOKEN is not {DISPATCH_TOKEN}")
    found += [
        f"{name}: the dispatch step does not run {call}"
        for call in DISPATCH_CALL
        if call not in str(step.get("run", ""))
    ]
    found += [
        f"DISPATCH_FUNCTIONS does not run {arg}"
        for arg in DISPATCH_ARGS
        if arg not in functions
    ]
    return found


@pytest.mark.parametrize("name", DISPATCHERS)
def test_the_dispatch_step_holds_every_property(jobs, name):
    assert dispatch_problems(name, jobs[name], _functions()["DISPATCH_FUNCTIONS"]) == []


# Each case: a step mutation, a function-text mutation, and the problem it must name.
DISPATCH_RED = {
    "with PROMOTE_TOKEN": (
        lambda step: step["env"].update(GH_TOKEN="${{ secrets.PROMOTE_TOKEN }}"),
        lambda fns: fns,
        "GH_TOKEN is not",
    ),
    "without the function": (
        lambda step: step.update(
            run=step["run"].replace("dispatch_newer_green", "true")
        ),
        lambda fns: fns,
        "does not run dispatch_newer_green",
    ),
    "without a sha": (
        lambda step: None,
        lambda fns: fns.replace(' -f sha="$newest"', ""),
        "does not run -f sha=",
    ),
}


@pytest.mark.parametrize("name", DISPATCHERS)
@pytest.mark.parametrize("case", DISPATCH_RED, ids=list(DISPATCH_RED))
def test_each_broken_dispatch_property_is_named(jobs, case, name):
    mutate_step, mutate_functions, expected = DISPATCH_RED[case]
    job = copy.deepcopy(jobs[name])
    mutate_step(_step(job, DISPATCH_STEP))
    functions = mutate_functions(_functions()["DISPATCH_FUNCTIONS"])
    found = dispatch_problems(name, job, functions)
    assert any(expected in line for line in found), found


def _dispatch(tmp_path, jobs, name: str, green: list[str], **env: str):
    script = _step(jobs[name], DISPATCH_STEP)["run"]
    settled = {DISPATCHERS[name]: T}
    result, calls = _run(
        tmp_path, script, FAKE_GREEN="\n".join(green), **settled, **env
    )
    return result, [call for call in calls if not call.startswith("gh api")]


@pytest.mark.parametrize("name", DISPATCHERS)
def test_a_green_commit_newer_than_t_dispatches_promote_for_it(tmp_path, jobs, name):
    result, calls = _dispatch(tmp_path, jobs, name, [NEWER, T, OLDER])
    assert result.returncode == 0, result.stderr
    assert calls == [DISPATCH]


@pytest.mark.parametrize("name", DISPATCHERS)
@pytest.mark.parametrize(
    "green",
    [[T, OLDER], [NEWER, OLDER]],
    ids=["t-is-newest", "t-outside-the-list"],
)
def test_no_newer_green_commit_does_not_dispatch(tmp_path, jobs, green, name):
    result, calls = _dispatch(tmp_path, jobs, name, green)
    assert result.returncode == 0, result.stderr
    assert calls == []


@pytest.mark.parametrize("name", DISPATCHERS)
@pytest.mark.parametrize(
    "green, env",
    [([NEWER, T], {"FAKE_GH_FAIL": "api"}), ([], {})],
    ids=["failed-read", "empty-list"],
)
def test_an_unreadable_run_list_fails_and_does_not_dispatch(
    tmp_path, jobs, green, env, name
):
    result, calls = _dispatch(tmp_path, jobs, name, green, **env)
    assert result.returncode != 0
    assert calls == []
