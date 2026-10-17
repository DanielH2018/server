"""The `promote` workflow's verification half: `verify`, `settle` and the dispatched `sha`.

`test_promote_workflow.py`'s `problems` holds every job's guard, permissions, checkouts and
handoff, these jobs included. This module adds the red fixtures for them that slice D2.5 lists
(docs/target-architecture/deploy/d2-first-leaf.md, *Steps, as PRs*, item 9), and the
properties only these two jobs have: each runs once `prod` has moved, whatever the jobs before
it concluded, neither joins the `promote` concurrency group, and their timeouts sit inside the
wait `decide` makes on a verification in flight.

The three limits move together (docs/target-architecture/deploy/d4-ordering.md, *The cap*):
`verify`'s `timeout-minutes` holds the capped derived timeout, `decide`'s in-flight wait holds
`verify`'s and `settle`'s limits plus runner queueing, and the `promote` job holds that wait
inside GitHub's 6-hour job limit. A `verify` its job limit stops leaves no verdict, and
`settle` writes `<T> unverified`, a hold only an operator clears.

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

import chained_timeout
from deploy_tools.promote_lib import verify_promotion as promote_verify

# `verify` and `settle` run only once the `tag` job's move step reports that `prod` moved.
MOVED_GATE = "needs.tag.outputs.moved == 'true'"
# Weighed even when an earlier job failed or was cancelled, so a moved `prod` always settles.
ALWAYS = "always()"
PRIOR_DIGEST = "needs.promote.outputs.prior_digest"
# Runner queueing between a run's jobs, which `decide`'s in-flight wait also covers.
RUNNER_QUEUE_S = 10 * 60
# The Discord webhook secret, and the tokens a step that holds it must not.
WEBHOOK = "DEPLOY_DISCORD_WEBHOOK"
TOKENS = ("secrets.PROMOTE_TOKEN", "github.token", "secrets.GITHUB_TOKEN")
# GitHub stops a job on a hosted runner after 6 hours.
HOSTED_JOB_LIMIT_MINUTES = 360


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def flux_version() -> str:
    return _load(FLUX_DEFAULTS)["flux_version"]


def _suspended_post_problems(promote: dict) -> list[str]:
    """D5.1d's post: a step after Decide sends `decide`'s hold with the webhook and no token."""
    found = []
    steps = _steps(promote)
    posting = [s for s in steps if WEBHOOK in s.get("env", {})]
    if not any(
        s.get("env", {}).get("HOLD") == "${{ steps.decide.outputs.suspended_hold }}"
        and "steps.decide.outputs.suspended_hold != ''" in str(s.get("if", ""))
        for s in posting
    ):
        found.append("promote: no webhook step posts decide's suspended_hold output")
    # The webhook and a token that writes never share a step, so no code that posts can write
    # DEPLOY_HOLD, and no code that writes it can read the webhook.
    for step in posting:
        if any(token in str(step.get("env")) for token in TOKENS):
            found.append(
                f"promote: the step {step.get('name')!r} holds the webhook and a token"
            )
    return found


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
        # The DECIDED marker in scripts/deploy_tools/promote_lib/verify_promotion.py: a pending job in the
        # group can be evicted, so `decide` waits on a verification in flight instead.
        if "concurrency" in job:
            found.append(f"{name}: joins a concurrency group")
    # `tag` moves `prod` only while it still names the digest `decide` read.
    move = next((s for s in _steps(jobs.get("tag", {})) if s.get("id") == "move"), {})
    if PRIOR_DIGEST not in str(move.get("env")) or "manifests/prod" not in str(
        move.get("run")
    ):
        found.append(
            "tag: does not compare prod's live digest with decide's before moving"
        )
    verify = jobs.get("verify", {})
    # A status an earlier promotion of T left must not count, so verify reads from the move.
    if "needs.tag.outputs.moved_at" not in str(
        _steps(verify)
    ) or "--moved-at" not in str(_steps(verify)):
        found.append("verify: does not bound T's statuses by the time prod moved")
    if "needs.promote.outputs.target" not in str(_steps(verify)):
        found.append("verify: does not take T from promote's outputs")
    # Without `no_reset` reaching settle's NO_RESET, a failed forward-only revision resets
    # `prod` to `last-good`, the downgrade the hold without a reset exists to prevent. Without
    # `stateful` reaching STATEFUL, a failed snapshot-role revision resets onto data it may
    # have migrated (D5.2b1).
    outputs = jobs.get("promote", {}).get("outputs", {})
    for key, env in (("no_reset", "NO_RESET"), ("stateful", "STATEFUL")):
        if outputs.get(key) != f"${{{{ steps.decide.outputs.{key} }}}}":
            found.append(f"promote: output {key} does not come from the decide step")
        if not any(
            s.get("env", {}).get(env) == f"${{{{ needs.promote.outputs.{key} }}}}"
            for s in _steps(jobs.get("settle", {}))
        ):
            found.append(f"settle: no step takes {env} from promote's {key} output")
    # The post runs only on the value settle wrote, so a broken link posts nothing.
    settle_out = jobs.get("settle", {}).get("outputs", {})
    if settle_out.get("stateful_hold") != "${{ steps.apply.outputs.stateful_hold }}":
        found.append("settle: output stateful_hold does not come from the apply step")
    if not any(
        s.get("env", {}).get("HOLD") == "${{ needs.settle.outputs.stateful_hold }}"
        for s in _steps(jobs.get("post", {}))
    ):
        found.append("post: no step takes HOLD from settle's stateful_hold output")
    found += _suspended_post_problems(jobs.get("promote", {}))
    minutes = {name: jobs.get(name, {}).get("timeout-minutes", 0) for name in jobs}
    if minutes["verify"] * 60 <= chained_timeout.VERIFY_CAP_S + promote_verify.POLL_S:
        found.append(
            "verify: its timeout-minutes cannot hold the capped derived timeout, so the "
            "job limit would stop it with no verdict"
        )
    if (
        minutes["verify"] + minutes["settle"]
    ) * 60 + RUNNER_QUEUE_S > promote_verify.IN_FLIGHT_WAIT_S:
        found.append("verify and settle can outlast decide's in-flight wait")
    if minutes["promote"] * 60 <= promote_verify.IN_FLIGHT_WAIT_S:
        found.append("promote: its timeout-minutes cannot hold decide's in-flight wait")
    if minutes["promote"] > HOSTED_JOB_LIMIT_MINUTES:
        found.append("promote: its timeout-minutes exceeds GitHub's 6-hour job limit")
    return found


def test_the_verification_jobs_hold_every_property(workflow):
    assert verification_problems(workflow) == []


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
    wf["jobs"]["verify"]["timeout-minutes"] = 346


def _verify_below_the_cap(wf):
    """D2.5's 25 minutes, sized for one tag's timeout before the chained timeout."""
    wf["jobs"]["verify"]["timeout-minutes"] = 25


def _promote_past_the_job_limit(wf):
    wf["jobs"]["promote"]["timeout-minutes"] = 361


def _a_short_promote_job(wf):
    wf["jobs"]["promote"]["timeout-minutes"] = 15


def _verify_without_the_move_time(wf):
    step = next(s for s in wf["jobs"]["verify"]["steps"] if s.get("id") == "verify")
    del step["env"]["MOVED_AT"]


def _tag_moves_without_comparing(wf):
    step = next(s for s in wf["jobs"]["tag"]["steps"] if s.get("id") == "move")
    del step["env"]["PRIOR_DIGEST"]


def _settle_reads_a_misspelt_no_reset(wf):
    step = next(
        s for s in wf["jobs"]["settle"]["steps"] if s.get("name") == "Apply the verdict"
    )
    step["env"]["NO_RESET"] = "${{ needs.promote.outputs.noreset }}"


def _promote_without_no_reset(wf):
    del wf["jobs"]["promote"]["outputs"]["no_reset"]


def _settle_reads_a_misspelt_stateful(wf):
    step = next(
        s for s in wf["jobs"]["settle"]["steps"] if s.get("name") == "Apply the verdict"
    )
    step["env"]["STATEFUL"] = "${{ needs.promote.outputs.statefull }}"


def _promote_without_stateful(wf):
    del wf["jobs"]["promote"]["outputs"]["stateful"]


def _post_reads_no_hold(wf):
    step = next(s for s in wf["jobs"]["post"]["steps"] if "HOLD" in s.get("env", {}))
    step["env"]["HOLD"] = "${{ needs.settle.outputs.hold }}"


def _suspended_post(wf) -> dict:
    return next(
        s for s in wf["jobs"]["promote"]["steps"] if WEBHOOK in s.get("env", {})
    )


def _suspended_post_reads_a_misspelt_hold(wf):
    _suspended_post(wf)["env"]["HOLD"] = "${{ steps.decide.outputs.suspended }}"


def _suspended_post_gains_the_promote_token(wf):
    _suspended_post(wf)["env"]["GH_TOKEN"] = "${{ secrets.PROMOTE_TOKEN }}"


def _decide_gains_the_webhook(wf):
    step = next(s for s in wf["jobs"]["promote"]["steps"] if s.get("id") == "decide")
    step["env"][WEBHOOK] = "${{ secrets.DEPLOY_DISCORD_WEBHOOK }}"


OWN_RED = {
    "the suspended post reads a misspelt output": (
        _suspended_post_reads_a_misspelt_hold,
        "promote: no webhook step posts decide's suspended_hold",
    ),
    "the suspended post holds PROMOTE_TOKEN": (
        _suspended_post_gains_the_promote_token,
        "promote: the step 'Post the suspended-changed hold' holds the webhook and a token",
    ),
    "Decide holds the webhook": (
        _decide_gains_the_webhook,
        "promote: the step 'Decide' holds the webhook and a token",
    ),
    "settle reads a misspelt stateful": (
        _settle_reads_a_misspelt_stateful,
        "settle: no step takes STATEFUL",
    ),
    "promote without the stateful output": (
        _promote_without_stateful,
        "promote: output stateful does not come",
    ),
    "post reads a hold settle never sets": (
        _post_reads_no_hold,
        "post: no step takes HOLD",
    ),
    "settle reads a misspelt no_reset": (
        _settle_reads_a_misspelt_no_reset,
        "settle: no step takes NO_RESET",
    ),
    "promote without the no_reset output": (
        _promote_without_no_reset,
        "promote: output no_reset does not come",
    ),
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
    "a verify limit below the capped timeout": (
        _verify_below_the_cap,
        "verify: its timeout-minutes cannot hold the capped derived timeout",
    ),
    "a promote job past GitHub's job limit": (
        _promote_past_the_job_limit,
        "promote: its timeout-minutes exceeds GitHub's 6-hour job limit",
    ),
    "a promote job shorter than the wait": (
        _a_short_promote_job,
        "promote: its timeout-minutes cannot hold",
    ),
    "a verify that reads statuses from before the move": (
        _verify_without_the_move_time,
        "verify: does not bound T's statuses",
    ),
    "a tag that moves prod without comparing": (
        _tag_moves_without_comparing,
        "tag: does not compare prod's live digest",
    ),
}


@pytest.mark.parametrize("case", OWN_RED, ids=list(OWN_RED))
def test_each_broken_verification_job_property_is_named(workflow, case):
    mutate, expected = OWN_RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = verification_problems(broken)
    assert any(expected in line for line in found), found
