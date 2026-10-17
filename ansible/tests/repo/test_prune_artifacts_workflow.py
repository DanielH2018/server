"""The `prune-artifacts` workflow deletes old artifact versions under promote's own rules.

It holds its master-ref check, its permissions, its own concurrency group, and a cosign
installer step equal to promote's, so the two pins cannot drift apart. `prune_problems` names
every broken property; the real file must produce none, and each red fixture breaks one.

Run: uv run pytest ansible/tests/repo/test_prune_artifacts_workflow.py
"""

import copy

import pytest
from _helpers import REPO
from test_promote_workflow import (
    PROMOTE_REF,
    REF_GUARD,
    WORKFLOW,
    _checkouts,
    _load,
    _steps,
    _triggers,
    _uses,
)

PRUNE_WORKFLOW = REPO / ".github" / "workflows" / "prune-artifacts.yml"
PRUNE_PERMISSIONS = {"contents": "read", "packages": "write"}
PRUNE_COMMAND = "promote.py prune"


def _cosign_steps(wf: dict) -> list[dict]:
    return [
        s
        for job in (wf.get("jobs") or {}).values()
        for s in _steps(job)
        if _uses(s, "sigstore/cosign-installer")
    ]


def prune_problems(wf: dict, promote_wf: dict) -> list[str]:
    """Every broken property of the prune workflow, as one line each.

    `promote_wf` supplies the cosign installer step the prune job must repeat exactly, the
    action's commit SHA and `cosign-release` both.
    """
    found = []
    triggers = _triggers(wf)
    if "schedule" not in triggers or "workflow_dispatch" not in triggers:
        found.append("prune: does not trigger on both schedule and workflow_dispatch")
    if wf.get("permissions") != {}:
        found.append("prune: the workflow grants permissions above the job level")
    jobs = wf.get("jobs") or {}
    if list(jobs) != ["prune"]:
        return found + [f"prune: the jobs are {sorted(jobs)}, not ['prune']"]
    job = jobs["prune"]
    if REF_GUARD not in " ".join(str(job.get("if", "")).split()):
        found.append(f"prune: its `if` lacks the master-ref guard {REF_GUARD}")
    if job.get("runs-on") != "ubuntu-26.04":
        found.append(f"prune: runs on {job.get('runs-on')!r}")
    if job.get("permissions") != PRUNE_PERMISSIONS:
        found.append(f"prune: permissions are {job.get('permissions')}")
    if job.get("concurrency") != {"group": "prune", "cancel-in-progress": False}:
        found.append(f"prune: its concurrency is {job.get('concurrency')}")
    checkouts = _checkouts(job)
    args = (checkouts[0].get("with") or {}) if len(checkouts) == 1 else {}
    if args.get("ref") != PROMOTE_REF or args.get("fetch-depth") != 0:
        found.append(f"prune: does not check out {PROMOTE_REF} with fetch-depth: 0")
    if not any(PRUNE_COMMAND in str(s.get("run", "")) for s in _steps(job)):
        found.append(f"prune: runs no `{PRUNE_COMMAND}`")
    cosign, reference = _cosign_steps(wf), _cosign_steps(promote_wf)
    if len(cosign) != 1 or any(cosign[0] != step for step in reference):
        found.append("prune: its cosign installer step differs from promote.yml's")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def prune_workflow() -> dict:
    return _load(PRUNE_WORKFLOW)


def test_the_prune_workflow_holds_every_property(prune_workflow, workflow):
    assert prune_problems(prune_workflow, workflow) == []
    assert len(_cosign_steps(workflow)) == 2


def _prune_in_the_promote_group(wf):
    wf["jobs"]["prune"]["concurrency"]["group"] = "promote"


def _prune_without_the_ref_check(wf):
    wf["jobs"]["prune"]["if"] = "true"


def _prune_with_a_wider_permission(wf):
    wf["jobs"]["prune"]["permissions"]["id-token"] = "write"


def _prune_with_a_drifted_cosign_release(wf):
    _cosign_steps(wf)[0]["with"]["cosign-release"] = "v3.1.4"


def _prune_with_a_drifted_cosign_installer(wf):
    step = _cosign_steps(wf)[0]
    step["uses"] = "sigstore/cosign-installer@" + "0" * 40


def _prune_with_a_shallow_checkout(wf):
    _checkouts(wf["jobs"]["prune"])[0]["with"]["fetch-depth"] = 1


PRUNE_RED = {
    "prune in the promote group": (
        _prune_in_the_promote_group,
        "prune: its concurrency is",
    ),
    "prune without the ref check": (
        _prune_without_the_ref_check,
        "prune: its `if` lacks the master-ref guard",
    ),
    "prune with a wider permission": (
        _prune_with_a_wider_permission,
        "prune: permissions are",
    ),
    "prune with a drifted cosign-release": (
        _prune_with_a_drifted_cosign_release,
        "prune: its cosign installer step differs",
    ),
    "prune with a drifted cosign installer": (
        _prune_with_a_drifted_cosign_installer,
        "prune: its cosign installer step differs",
    ),
    "prune with a shallow checkout": (
        _prune_with_a_shallow_checkout,
        "prune: does not check out",
    ),
}


@pytest.mark.parametrize("case", PRUNE_RED, ids=list(PRUNE_RED))
def test_each_broken_prune_property_is_named(prune_workflow, workflow, case):
    mutate, expected = PRUNE_RED[case]
    broken = copy.deepcopy(prune_workflow)
    mutate(broken)
    found = prune_problems(broken, workflow)
    assert any(expected in line for line in found), found
