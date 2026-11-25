"""The phase order, the step headers, and nothing else.

Each phase is a function in its own module; this is the only place that knows the sequence.
`run` turns the Outcome a phase raises into a return value, so main() has one thing to print
and one thing to annotate.

THE STEP HEADERS ARE OWNED HERE, ALL OF THEM. `== N/M  <label>` used to be printed by each
phase with `/6` written out seven times against eight prints, so the denominator was a
constant nobody could check. `_STEPS` is now the single ordered list and `M` is its length,
which makes the count true by construction. `== arm` and `== 0/M` stay outside the numbering
because they are conditional on `--arm-merge` and `--await-merge`; a landing without them
still runs steps 1 to M.

WHAT EACH PHASE READS AND WRITES on the shared `Landing`. The state is threaded through every
phase, so this table is the contract no signature states. `classification` is written once
and frozen; `plane` is a read-only property over it:

| Phase | Reads | Writes |
|---|---|---|
| `merge.arm_merge` | `opts.subject`, `opts.require_author` | `direct_merge_subject` |
| `classify.refuse_stale_code_before_merge` (when `direct_merge_subject` is set; it may move the landing to a fresh checkout) | `opts.pr`, `opts.primary`, `tools.stale_code` | -- |
| `merge.await_merge` | `opts.merge_timeout`, `opts.merge_poll`, the head's pins (the pin gate) | -- |
| `classify.resolve` | `opts.pr` | `merge_sha`, `ledger.t_merged`, `ledger.merge_sha` |
| `classify.refuse_stale_code` | `merge_sha`, `opts.since`, `opts.primary`, `tools.stale_code` | -- |
| `closing_refs.close_stranded` | `opts.pr` | `warnings` |
| `classify.classify` | `merge_sha`, `opts.since`, `opts.primary` | `classification` (frozen: the plane note and its inputs), `resolved_tags`, `needs_diff` |
| `classify.reaches_host_inputs` (in step 1) | `classification`, `needs_diff` | `host_candidate` |
| `classify.shortcut_if_nothing` | `resolved_tags`, `plane`, `classification`, `needs_diff`, `host_candidate` | -- |
| `slots.held` (from step 2 to the verdict) | `opts.merge_timeout`, `opts.merge_poll`, `ledger.t_start` | -- |
| `ci.wait_master_ci` | `merge_sha`, `opts.ci_timeout` | `ledger.t_ci` |
| `host_wait.host_targets` (in step 2, only when `host_candidate`) | `host_candidate`, `merge_sha`, `opts.primary` | `host_targets`, and `host_candidate` cleared when none differ |
| `deploy.deploy_phase` | `resolved_tags`, `needs_diff`, `host_targets`, `classification`, `opts.since`, `merge_sha` | `resolved_tags`, `named_k8s_tags`, `flux_tags`, `suspended_tags`, `deployed_at`, `ledger.tags_label`, `ledger.cause`, `ledger.t_deploy` |
| `host_wait.wait_for_hosts` (step 4) | `host_targets`, `merge_sha`, `opts.pr`, `opts.primary` | -- |
| `health_verdict.health` | `resolved_tags`, `host_targets`, `flux_tags`, `suspended_tags`, `deployed_at`, `plane`, `classification` | `ledger.cause` |

Every phase may end the landing by raising an `Outcome`, and the last one always does.
"""

import contextlib
from collections.abc import Callable


from deploy_tools.land_lib import (
    batch,
    ci,
    classify,
    closing_refs,
    deploy,
    health_verdict,
    host_wait,
    merge,
    slots,
    turn,
)
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say


def _step_resolve(ln: Landing) -> None:
    """Step 1 and 1½: the merge commit, any issue GitHub left open, then what this PR reaches."""
    classify.resolve(ln)
    classify.refuse_stale_code(ln)
    closing_refs.close_stranded(ln)
    classify.classify(ln)
    ln.host_candidate = classify.reaches_host_inputs(ln)
    classify.shortcut_if_nothing(ln)


def _step_ci(ln: Landing) -> None:
    """Wait for master CI on the merge commit, stamp when it answered, then name its hosts.

    The host render waits for a green CI because it runs the merge commit's code, which the
    resolve step must not (`classify.reaches_host_inputs`). The first parent is an earlier
    master commit, so CI has passed on the code that renders the parent too.
    """
    ci.wait_master_ci(ln, ln.merge_sha)
    ln.ledger.t_ci = ln.tools.clock()
    if not ln.host_candidate:
        return
    render = ln.tools.host_targets or host_wait.host_targets
    ln.host_targets = render(ln.opts.primary, ln.merge_sha)
    if not ln.host_targets:
        # The paths only looked like host inputs. Decide again, now that no host is a reason.
        ln.host_candidate = False
        classify.shortcut_if_nothing(ln)


def _step_hosts(ln: Landing) -> None:
    """Wait for every host this merge's package changes to apply it, or end the landing."""
    if not ln.host_targets:
        say("no host package changed; nothing to wait for")
        return
    wait = ln.tools.wait_for_hosts or host_wait.wait_for_hosts
    wait(ln, ln.host_targets, ln.merge_sha)


# The callables are bound at import: a test that monkeypatches `ci.preflight` does not reach the
# pipeline. Patch an entry of `_STEPS` instead.
_STEPS: tuple[tuple[str, Callable[[Landing], None]], ...] = (
    ("resolving PR #{pr}", _step_resolve),
    ("waiting for master CI", _step_ci),
    ("waiting on the promotion", deploy.deploy_phase),
    ("waiting for hosts to apply", _step_hosts),
    ("health verdict", health_verdict.health),
)

STEP_COUNT = len(_STEPS)
_SLOT_FROM_STEP = 2


def run(ln: Landing) -> Outcome:
    """Run every phase in order; the Outcome that ended it."""
    try:
        _phases(ln)
    except Outcome as outcome:
        ln.ledger.verdict = outcome.verdict or ""
        outcome.add_notes(ln.warnings)
        return outcome
    # Unreachable (the last step is `NoReturn`). Returned, not raised, because `run`'s `except
    # Outcome` would not catch it and land.py would show a traceback.
    ln.ledger.verdict = Verdict.REFUSED
    return Outcome(
        1,
        "the last phase must end the landing",
        verdict=Verdict.REFUSED,
        error="pipeline fell through",
    )


def _phases(ln: Landing) -> None:
    if not ln.opts.primary.is_dir():
        ln.die(f"cannot cd to {ln.opts.primary}", 1)
    if ln.opts.arm_merge:
        print(f"== arm  arming PR #{ln.opts.pr}'s merge")
        merge.arm_merge(ln)
    if ln.opts.await_merge:
        how = (
            "merging it directly once its CI is green"
            if ln.opts.arm_merge
            else "polling for a merge made elsewhere"
        )
        print(f"== 0/{STEP_COUNT}  waiting for PR #{ln.opts.pr} to merge ({how})")
        if ln.direct_merge_subject:
            classify.refuse_stale_code_before_merge(ln)
        with turn.held(ln) as take_turn:
            batch.land(ln, take_turn)
    with contextlib.ExitStack() as holding:
        for number, (label, phase) in enumerate(_STEPS, 1):
            print(f"== {number}/{STEP_COUNT}  {label.format(pr=ln.opts.pr)}")
            if number == _SLOT_FROM_STEP:
                holding.enter_context(slots.held(ln))
            phase(ln)
