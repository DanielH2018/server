"""The `promote` workflow's verification half: `verify`, `settle` and the dispatched `sha`.

`test_promote_workflow.py`'s `problems` holds every job's guard, permissions, checkouts and
handoff, these jobs included. This module adds the red fixtures for them that slice D2.5 lists
(docs/target-architecture/deploy/d2-first-leaf.md, *Steps, as PRs*, item 9), and the
properties only these two jobs have: each runs once `prod` has moved, whatever the jobs before
it concluded, neither joins the `promote` concurrency group, and their timeouts sit inside the
wait `decide` makes on a verification in flight.

Run: uv run pytest ansible/tests/repo/test_promote_workflow_verify.py
"""

import copy

import pytest
from test_promote_workflow import (
    FLUX_DEFAULTS,
    WORKFLOW,
    _checkouts,
    _load,
    _steps,
    problems,
)

import verify_promotion

# `verify` and `settle` run only once the `tag` job's move step reports that `prod` moved.
MOVED_GATE = "needs.tag.outputs.moved == 'true'"
# Weighed even when an earlier job failed or was cancelled, so a moved `prod` always settles.
ALWAYS = "always()"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def flux_version() -> str:
    return _load(FLUX_DEFAULTS)["flux_version"]


def verification_problems(wf: dict) -> list[str]:
    """The properties of `verify` and `settle` that `problems` does not hold, one line each."""
    jobs = wf.get("jobs") or {}
    found = []
    for name in ("verify", "settle"):
        job = jobs.get(name, {})
        condition = " ".join(str(job.get("if", "")).split())
        for gate in (ALWAYS, MOVED_GATE):
            if gate not in condition:
                found.append(f"{name}: its `if` lacks {gate}")
        # The DECIDED marker in scripts/deploy_tools/verify_promotion.py: a pending job in the
        # group can be evicted, so `decide` waits on a verification in flight instead.
        if "concurrency" in job:
            found.append(f"{name}: joins a concurrency group")
    verify = jobs.get("verify", {})
    if "needs.promote.outputs.target" not in str(_steps(verify)):
        found.append("verify: does not take T from promote's outputs")
    minutes = {name: jobs.get(name, {}).get("timeout-minutes", 0) for name in jobs}
    if (minutes["verify"] + minutes["settle"]) * 60 > verify_promotion.IN_FLIGHT_WAIT_S:
        found.append("verify and settle can outlast decide's in-flight wait")
    if minutes["promote"] * 60 <= verify_promotion.IN_FLIGHT_WAIT_S:
        found.append("promote: its timeout-minutes cannot hold decide's in-flight wait")
    return found


def test_the_verification_jobs_hold_every_property(workflow):
    assert verification_problems(workflow) == []


def test_verifys_timeout_holds_the_timeout_derived_for_a_tag_at_the_rollout_default():
    """bento-pdf's Kustomization, rendered at `manifests_rollout_timeout_default`."""
    from renderer.renderer_lib.bundle import tag_kustomization

    defaults = _load(
        WORKFLOW.parents[2] / "ansible/roles/k8s/manifests/defaults/main.yml"
    )
    doc = tag_kustomization(
        "bento-pdf", False, defaults["manifests_rollout_timeout_default"]
    )
    minutes = _load(WORKFLOW)["jobs"]["verify"]["timeout-minutes"]
    assert (
        verify_promotion.promote_timeout([doc]) + verify_promotion.POLL_S < minutes * 60
    )


def _settle_checks_out(wf):
    checkout = copy.deepcopy(_checkouts(wf["jobs"]["render"])[0])
    wf["jobs"]["settle"]["steps"].insert(0, checkout)


def _verify_holds_packages_write(wf):
    wf["jobs"]["verify"]["permissions"]["packages"] = "write"


def _render_before_ancestry(wf):
    steps = wf["jobs"]["render"]["steps"]
    build = next(s for s in steps if s.get("id") == "build")
    steps.remove(build)
    steps.insert(1, build)


def _ancestry_reads_github_sha(wf):
    step = next(s for s in wf["jobs"]["render"]["steps"] if s.get("id") == "ancestry")
    step["env"]["WANTED"] = "${{ github.sha }}"


def _render_without_the_ci_recheck(wf):
    steps = wf["jobs"]["render"]["steps"]
    steps[:] = [s for s in steps if s.get("id") != "ci-recheck"]


def _verify_reads_the_trigger(wf):
    step = next(s for s in wf["jobs"]["verify"]["steps"] if s.get("id") == "verify")
    step["env"]["TARGET"] = "${{ github.event.workflow_run.head_sha }}"


def _settle_with_id_token(wf):
    wf["jobs"]["settle"]["permissions"]["id-token"] = "write"


RED = {
    "settle checks out code": (_settle_checks_out, "settle: checks out the repository"),
    "verify holds packages: write": (
        _verify_holds_packages_write,
        "verify: permissions are",
    ),
    "a render step before the ancestry step": (
        _render_before_ancestry,
        "render: checks ancestry after the build step",
    ),
    "an ancestry step that reads github.sha": (
        _ancestry_reads_github_sha,
        "render: the ancestry step does not read inputs.sha",
    ),
    "a render with no CI re-check": (
        _render_without_the_ci_recheck,
        "render: runs no `promote.py ci-verdict`",
    ),
    "verify waits on the triggering commit": (
        _verify_reads_the_trigger,
        "a step after decide reads",
    ),
    "settle with id-token: write": (_settle_with_id_token, "settle: permissions"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_verification_property_is_named(workflow, flux_version, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = problems(broken, flux_version)
    assert any(expected in line for line in found), found


def _settle_in_the_promote_group(wf):
    wf["jobs"]["settle"]["concurrency"] = {
        "group": "promote",
        "cancel-in-progress": False,
    }


def _verify_without_always(wf):
    job = wf["jobs"]["verify"]
    job["if"] = job["if"].replace(ALWAYS, "true")


def _settle_without_the_moved_gate(wf):
    job = wf["jobs"]["settle"]
    job["if"] = job["if"].replace(MOVED_GATE, "true")


def _verify_outlasting_the_wait(wf):
    wf["jobs"]["verify"]["timeout-minutes"] = 40


def _a_short_promote_job(wf):
    wf["jobs"]["promote"]["timeout-minutes"] = 15


OWN_RED = {
    "settle in the promote group": (
        _settle_in_the_promote_group,
        "settle: joins a concurrency group",
    ),
    "verify without always()": (
        _verify_without_always,
        "verify: its `if` lacks always()",
    ),
    "settle without the moved gate": (
        _settle_without_the_moved_gate,
        "settle: its `if` lacks needs.tag.outputs.moved",
    ),
    "a verify that outlasts decide's wait": (
        _verify_outlasting_the_wait,
        "can outlast decide's in-flight wait",
    ),
    "a promote job shorter than the wait": (
        _a_short_promote_job,
        "promote: its timeout-minutes cannot hold",
    ),
}


@pytest.mark.parametrize("case", OWN_RED, ids=list(OWN_RED))
def test_each_broken_verification_job_property_is_named(workflow, case):
    mutate, expected = OWN_RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = verification_problems(broken)
    assert any(expected in line for line in found), found
