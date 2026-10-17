"""The `promote` workflow's window-end cron run reaches a move of `prod`, and the reset skips the window.

D5.2b2 (docs/target-architecture/deploy/d5-bulk-move.md, *No promotion runs into the weekly
reboot*). Inside the weekly reboot's no-promotion window `decide` ends `deferred`, and the
`schedule:` run at the window's end is what promotes the commits merged during it. That run
skips render, images and publish, so the jobs after them need conditions of their own.
If one is missing, the run decides and never moves `prod`, and CI stays green. The
`reset_to_last_good` jobs must reach none of the jobs that read the window.

`problems` names every broken property. The real file must produce none, and each red
fixture is the real file with one property broken.

Run: uv run pytest ansible/tests/repo/test_promote_workflow_schedule.py
"""

import copy

import pytest
from _helpers import REPO
from lib import yaml_fast

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"

SCHEDULED = "github.event_name == 'schedule'"
# `promote` runs on a skipped publish for the scheduled run alone.
SCHEDULED_PROMOTE = f"({SCHEDULED} && needs.publish.result == 'skipped')"
# A status-check function in an `if` replaces GitHub's implicit success(), which reads every
# ancestor in `needs`. Without one, the scheduled run's skipped publish skips the job. Only
# !cancelled() is accepted: always() would also move `prod` on a cancelled run.
STATUS_CHECK = "!cancelled()"
# `tag` reads `promote`'s result itself, because the skipped publish is its ancestor too.
TAG_RESULT = (STATUS_CHECK, "needs.promote.result == 'success'")
AFTER_PROMOTE = ("tag", "verify", "settle", "post")
# `decide` reads the window in `promote`, and the jobs after it act on its verdict. A reset job
# that needed one of these would skip with the window, or wait on it.
WINDOW_JOBS = frozenset({"render", "images", "publish", "promote", *AFTER_PROMOTE})
RESET_JOBS = ("reset-check", "reset-move")


def _condition(job: dict) -> str:
    return " ".join(str(job.get("if", "")).split())


def _ancestors(jobs: dict, name: str) -> set[str]:
    needs = jobs.get(name, {}).get("needs") or []
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_ancestors(jobs, n) for n in direct))


def problems(wf: dict) -> list[str]:
    """The scheduled run's path to a move of `prod`, and the reset's path around the window."""
    jobs = wf.get("jobs") or {}
    found = []
    if SCHEDULED in _condition(jobs.get("render", {})):
        found.append("render: runs on the schedule, so it publishes an unchecked tip")
    promote = _condition(jobs.get("promote", {}))
    if SCHEDULED_PROMOTE not in promote:
        found.append(f"promote: its `if` lacks {SCHEDULED_PROMOTE}")
    if STATUS_CHECK not in promote:
        found.append(
            f"promote: its `if` lacks {STATUS_CHECK}, so the scheduled run skips it"
        )
    for guard in TAG_RESULT:
        if guard not in _condition(jobs.get("tag", {})):
            found.append(f"tag: its `if` lacks {guard}, so the scheduled run skips it")
    for name in AFTER_PROMOTE:
        if SCHEDULED not in _condition(jobs.get(name, {})):
            found.append(f"{name}: its `if` lacks {SCHEDULED}")
    for name in RESET_JOBS:
        if name not in jobs:
            found.append(f"{name}: the job is missing")
        elif crossed := sorted(_ancestors(jobs, name) & WINDOW_JOBS):
            found.append(f"{name}: needs {crossed}, which read the reboot window")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_scheduled_run_and_the_reset_paths_are_clean(workflow):
    assert problems(workflow) == []


def _replace(job: str, old: str, new: str):
    def mutate(wf):
        wf["jobs"][job]["if"] = wf["jobs"][job]["if"].replace(old, new)

    return mutate


def _render_on_the_schedule(wf):
    job = wf["jobs"]["render"]
    job["if"] = f"{SCHEDULED} || {job['if']}"


def _reset_needs_promote(wf):
    wf["jobs"]["reset-check"]["needs"] = "promote"


RED = {
    "tag without the schedule": (
        _replace("tag", f"{SCHEDULED} ||", ""),
        f"tag: its `if` lacks {SCHEDULED}",
    ),
    "settle without the schedule": (
        _replace("settle", f"{SCHEDULED} ||", ""),
        f"settle: its `if` lacks {SCHEDULED}",
    ),
    "tag on implicit success": (
        _replace("tag", "!cancelled() &&", ""),
        "tag: its `if` lacks !cancelled()",
    ),
    "promote on implicit success": (
        _replace("promote", "!cancelled() &&", ""),
        "promote: its `if` lacks !cancelled()",
    ),
    "promote on a published tip only": (
        _replace("promote", SCHEDULED_PROMOTE, "false"),
        f"promote: its `if` lacks {SCHEDULED_PROMOTE}",
    ),
    "render on the schedule": (_render_on_the_schedule, "render: runs on the schedule"),
    "a reset that needs promote": (
        _reset_needs_promote,
        "reset-check: needs ['images', 'promote', 'publish', 'render']",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_schedule_property_is_named(workflow, case):
    """The red halves. Each mutation must change the workflow, or it proves nothing."""
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    assert broken != workflow, f"{case}: the mutation left the workflow unchanged"
    found = problems(broken)
    assert any(expected in line for line in found), found
