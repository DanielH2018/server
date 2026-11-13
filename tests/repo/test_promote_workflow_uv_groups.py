"""Every `uv run` in the `promote` workflow installs no default dependency group.

The workflow env sets UV_NO_DEV and UV_NO_GROUP=lint, so an unreachable tool download in the
`dev` or `lint` group (actionlint-py's build, #7625) cannot block a promotion. No job or step
may override either, and no `uv` call may ask for a group.

Run: uv run pytest tests/repo/test_promote_workflow_uv_groups.py
"""

import copy
import re

import pytest
from test_promote_workflow import WORKFLOW, _load, _steps

DEV_GROUP_FLAG = re.compile(
    r"\buv (?:run|sync)\b[^\n]*--(?:dev|only-dev|group|all-groups)\b"
)


def group_problems(wf: dict) -> list[str]:
    """Every way the workflow lets `uv` install a dependency group, as one line each."""
    found = []
    if (wf.get("env") or {}).get("UV_NO_DEV") != "1":
        found.append(
            "the workflow env does not set UV_NO_DEV, so `uv run` installs the dev group"
        )
    if (wf.get("env") or {}).get("UV_NO_GROUP") != "lint":
        found.append(
            "the workflow env does not set UV_NO_GROUP=lint, so `uv run` installs the lint group"
        )
    for name, job in (wf.get("jobs") or {}).items():
        for scope in [job, *_steps(job)]:
            for var in ("UV_NO_DEV", "UV_NO_GROUP"):
                if var in (scope.get("env") or {}):
                    found.append(f"{name}: overrides {var}")
            if DEV_GROUP_FLAG.search(str(scope.get("run", ""))):
                found.append(f"{name}: a `uv` call asks for a dependency group")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


def test_the_promote_workflow_installs_no_group_is_clean(workflow):
    assert group_problems(workflow) == []


def _dev_group_installed(wf):
    del wf["env"]["UV_NO_DEV"]


def _lint_group_installed(wf):
    del wf["env"]["UV_NO_GROUP"]


def _render_job_reenables_dev(wf):
    wf["jobs"]["render"].setdefault("env", {})["UV_NO_DEV"] = ""


def _step_asks_for_a_group(wf):
    step = next(s for s in wf["jobs"]["render"]["steps"] if s.get("id") == "build")
    step["run"] = step["run"].replace("uv run python", "uv run --group dev python", 1)


RED = {
    "the dev group installed": (_dev_group_installed, "does not set UV_NO_DEV"),
    "the lint group installed": (
        _lint_group_installed,
        "does not set UV_NO_GROUP=lint",
    ),
    "render re-enables the dev group": (
        _render_job_reenables_dev,
        "render: overrides UV_NO_DEV",
    ),
    "a step asks for a group": (_step_asks_for_a_group, "render: a `uv` call asks for"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_way_to_install_a_group_is_flagged(workflow, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    assert broken != workflow, f"{case}: the mutation left the workflow unchanged"
    found = group_problems(broken)
    assert any(expected in line for line in found), found
