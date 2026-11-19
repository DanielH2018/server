"""The `promote` workflow's verification half: `verify`, `settle` and what they hand on.

`_promote_workflow_rows.py` holds every job's guard, scopes, checkouts and handoff. These rows
hold what only the verification jobs have: each runs once `prod` has moved, whatever the jobs
before it concluded, neither joins the `promote` concurrency group, and their timeouts sit inside
the wait `decide` makes on a verification in flight (ADR-0020).

The three limits move together. `verify`'s `timeout-minutes` holds the capped derived timeout,
`decide`'s in-flight wait holds `verify`'s and `settle`'s limits plus runner queueing, and the
`promote` job holds that wait inside GitHub's 6-hour job limit. A `verify` its job limit stops
leaves no verdict, and `settle` writes `<T> unverified`, a hold only an operator clears.
"""

import chained_timeout
from _promote_workflow import job, promote_row, step_by
from _row_table import Census
from _workflow_rows import PROMOTE, condition, judge, mutants, steps
from deploy_tools.promote_lib import verify_promotion

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
SUSPENDED_HOLD = "${{ steps.decide.outputs.suspended_hold }}"
# The jobs that write or record the hold `decide`'s `hold_form` names.
HOLD_FORM_READERS = ("verdict-status", "settle", "reconcile")


def _minutes(wf: dict, name: str) -> int:
    return job(wf, name).get("timeout-minutes", 0)


def _run_gate_problems(wf: dict) -> list[str]:
    return [
        f"{name}: its `if` lacks {gate}"
        for name in ("verify", "settle")
        for gate in (ALWAYS, MOVED_GATE)
        if gate not in condition(job(wf, name))
    ]


def _concurrency_problems(wf: dict) -> list[str]:
    return [
        f"{name}: joins a concurrency group"
        for name in ("verify", "settle")
        if "concurrency" in job(wf, name)
    ]


def _tag_compare_problems(wf: dict) -> list[str]:
    move = next((s for s in steps(job(wf, "tag")) if s.get("id") == "move"), {})
    if PRIOR_DIGEST not in str(move.get("env")) or "live_tag prod" not in str(
        move.get("run")
    ):
        return ["tag: does not compare prod's live digest with decide's before moving"]
    return []


def _verify_input_problems(wf: dict) -> list[str]:
    verify = str(steps(job(wf, "verify")))
    found = []
    if "needs.tag.outputs.moved_at" not in verify or "--moved-at" not in verify:
        found.append("verify: does not bound T's statuses by the time prod moved")
    if "needs.promote.outputs.target" not in verify:
        found.append("verify: does not take T from promote's outputs")
    return found


def _hold_form_problems(wf: dict) -> list[str]:
    found = []
    if job(wf, "promote").get("outputs", {}).get("hold_form") != (
        "${{ steps.decide.outputs.hold_form }}"
    ):
        found.append("promote: output hold_form does not come from the decide step")
    for name in HOLD_FORM_READERS:
        if not any(
            s.get("env", {}).get("HOLD_FORM")
            == "${{ needs.promote.outputs.hold_form }}"
            for s in steps(job(wf, name))
        ):
            found.append(
                f"{name}: no step takes HOLD_FORM from promote's hold_form output"
            )
    return found


def _post_hold_problems(wf: dict) -> list[str]:
    found = []
    if job(wf, "settle").get("outputs", {}).get("stateful_hold") != (
        "${{ steps.apply.outputs.stateful_hold }}"
    ):
        found.append("settle: output stateful_hold does not come from the apply step")
    if not any(
        s.get("env", {}).get("HOLD") == "${{ needs.settle.outputs.stateful_hold }}"
        for s in steps(job(wf, "post"))
    ):
        found.append("post: no step takes HOLD from settle's stateful_hold output")
    return found


def _webhook_steps(wf: dict) -> list[dict]:
    return [s for s in steps(job(wf, "promote")) if WEBHOOK in s.get("env", {})]


def _suspended_post_problems(wf: dict) -> list[str]:
    if any(
        s.get("env", {}).get("HOLD") == SUSPENDED_HOLD
        and "steps.decide.outputs.suspended_hold != ''" in str(s.get("if", ""))
        for s in _webhook_steps(wf)
    ):
        return []
    return ["promote: no webhook step posts decide's suspended_hold output"]


def _webhook_token_problems(wf: dict) -> list[str]:
    return [
        f"promote: the step {step.get('name')!r} holds the webhook and a token"
        for step in _webhook_steps(wf)
        if any(token in str(step.get("env")) for token in TOKENS)
    ]


def _verify_cap_problems(wf: dict) -> list[str]:
    if (
        _minutes(wf, "verify") * 60
        <= chained_timeout.VERIFY_CAP_S + verify_promotion.POLL_S
    ):
        return [
            "verify: its timeout-minutes cannot hold the capped derived timeout, so the "
            "job limit would stop it with no verdict"
        ]
    return []


def _in_flight_wait_problems(wf: dict) -> list[str]:
    if (
        _minutes(wf, "verify") + _minutes(wf, "settle")
    ) * 60 + RUNNER_QUEUE_S > verify_promotion.IN_FLIGHT_WAIT_S:
        return ["verify and settle can outlast decide's in-flight wait"]
    return []


def _promote_limit_problems(wf: dict) -> list[str]:
    found = []
    if _minutes(wf, "promote") * 60 <= verify_promotion.IN_FLIGHT_WAIT_S:
        found.append("promote: its timeout-minutes cannot hold decide's in-flight wait")
    if _minutes(wf, "promote") > HOSTED_JOB_LIMIT_MINUTES:
        found.append("promote: its timeout-minutes exceeds GitHub's 6-hour job limit")
    return found


def _replace_in_if(name: str, needle: str):
    def mutate(wf: dict) -> None:
        wf["jobs"][name]["if"] = wf["jobs"][name]["if"].replace(needle, "true")

    return mutate


def _set_minutes(name: str, minutes: int):
    def mutate(wf: dict) -> None:
        wf["jobs"][name]["timeout-minutes"] = minutes

    return mutate


def _settle_step(wf: dict) -> dict:
    return step_by(wf, "settle", name="Apply the verdict")


def _suspended_post(wf: dict) -> dict:
    return _webhook_steps(wf)[0]


PROMOTE_VERIFY_ROWS = (
    Census(
        **promote_row(
            name="promote-verify-and-settle-run-once-prod-moved",
            reason=(
                "`verify` and `settle` weigh `always()` and the `tag` job's `moved` output, so "
                "a moved `prod` always settles, and a `prod` that did not move runs neither."
            ),
            offence=judge(_run_gate_problems),
            red=mutants(
                PROMOTE,
                verify_without_always=_replace_in_if("verify", ALWAYS),
                settle_without_the_moved_gate=_replace_in_if("settle", MOVED_GATE),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-verify-and-settle-join-no-concurrency-group",
            reason=(
                "A pending job in the `promote` group can be evicted, so `decide` waits on a "
                "verification in flight instead, and `verify` and `settle` stay outside it."
            ),
            offence=judge(_concurrency_problems),
            red=mutants(
                PROMOTE,
                settle_in_the_promote_group=lambda wf: wf["jobs"]["settle"].update(
                    concurrency={"group": "promote", "cancel-in-progress": False}
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-tag-compares-prods-digest-before-moving",
            reason="`tag` moves `prod` only while it still names the digest `decide` read.",
            offence=judge(_tag_compare_problems),
            red=mutants(
                PROMOTE,
                a_tag_that_moves_prod_without_comparing=lambda wf: step_by(
                    wf, "tag", id="move"
                )["env"].pop("PRIOR_DIGEST"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-verify-reads-the-move-time-and-target",
            reason=(
                "A status an earlier promotion of T left must not count, so `verify` reads "
                "statuses from the move, and it takes T from `promote`'s outputs."
            ),
            offence=judge(_verify_input_problems),
            red=mutants(
                PROMOTE,
                a_verify_that_reads_statuses_from_before_the_move=lambda wf: step_by(
                    wf, "verify", id="verify"
                )["env"].pop("MOVED_AT"),
                a_verify_that_takes_no_target_from_promote=lambda wf: step_by(
                    wf, "verify", id="verify"
                )["env"].pop("TARGET"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-hands-hold-form-to-its-readers",
            reason=(
                "`decide` alone computes the hold form. Without `hold_form` reaching each "
                "reader's HOLD_FORM, `settle` and `reconcile` write no hold and "
                "`verdict-status` posts no status, so a failed revision stops no promotion."
            ),
            offence=judge(_hold_form_problems),
            red=mutants(
                PROMOTE,
                settle_reads_a_misspelt_hold_form=lambda wf: _settle_step(wf)[
                    "env"
                ].update(HOLD_FORM="${{ needs.promote.outputs.holdform }}"),
                reconcile_without_hold_form=lambda wf: step_by(
                    wf, "reconcile", id="apply"
                )["env"].pop("HOLD_FORM"),
                promote_without_the_hold_form_output=lambda wf: wf["jobs"]["promote"][
                    "outputs"
                ].pop("hold_form"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-post-takes-its-hold-from-settle",
            reason=(
                "The post runs only on the value `settle` wrote, so a broken link posts "
                "nothing."
            ),
            offence=judge(_post_hold_problems),
            red=mutants(
                PROMOTE,
                post_reads_a_hold_settle_never_sets=lambda wf: next(
                    s for s in wf["jobs"]["post"]["steps"] if "HOLD" in s.get("env", {})
                )["env"].update(HOLD="${{ needs.settle.outputs.hold }}"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-posts-decides-suspended-hold",
            reason=(
                "A step after Decide sends `decide`'s suspended_hold output with the webhook, "
                "so a suspended-changed hold reaches the operator."
            ),
            offence=judge(_suspended_post_problems),
            red=mutants(
                PROMOTE,
                the_suspended_post_reads_a_misspelt_output=lambda wf: _suspended_post(
                    wf
                )["env"].update(HOLD="${{ steps.decide.outputs.suspended }}"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-webhook-and-write-token-never-share-a-step",
            reason=(
                "No code that posts can write DEPLOY_HOLD, and no code that writes it can "
                "read the webhook. The scan covers every step that holds the webhook, so it "
                "catches a token added to the post step and a webhook added to the step that "
                "holds a token."
            ),
            offence=judge(_webhook_token_problems),
            red=mutants(
                PROMOTE,
                the_suspended_post_holds_PROMOTE_TOKEN=lambda wf: _suspended_post(wf)[
                    "env"
                ].update(GH_TOKEN="${{ secrets.PROMOTE_TOKEN }}"),
                Decide_holds_the_webhook=lambda wf: step_by(wf, "promote", id="decide")[
                    "env"
                ].update({WEBHOOK: "${{ secrets.DEPLOY_DISCORD_WEBHOOK }}"}),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-verify-timeout-holds-the-capped-derived-timeout",
            reason=(
                "`verify`'s `timeout-minutes` holds the capped derived timeout plus a poll. A "
                "`verify` its job limit stops leaves no verdict, and `settle` writes "
                "`<T> unverified`, a hold only an operator clears."
            ),
            offence=judge(_verify_cap_problems),
            red=mutants(
                PROMOTE,
                a_verify_limit_below_the_capped_timeout=_set_minutes("verify", 25),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-verify-and-settle-fit-inside-decides-wait",
            reason=(
                "`decide`'s in-flight wait holds `verify`'s and `settle`'s limits plus runner "
                "queueing, so a verification in flight always ends before `decide` gives up "
                "on it."
            ),
            offence=judge(_in_flight_wait_problems),
            red=mutants(
                PROMOTE, a_verify_that_outlasts_decides_wait=_set_minutes("verify", 346)
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-job-timeout-holds-the-wait-inside-githubs-limit",
            reason=(
                "The `promote` job holds `decide`'s in-flight wait, and stays inside GitHub's "
                "6-hour job limit."
            ),
            offence=judge(_promote_limit_problems),
            red=mutants(
                PROMOTE,
                a_promote_job_past_githubs_job_limit=_set_minutes("promote", 361),
                a_promote_job_shorter_than_the_wait=_set_minutes("promote", 15),
            ),
        )
    ),
)
