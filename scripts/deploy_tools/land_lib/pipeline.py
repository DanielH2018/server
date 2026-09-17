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

WHAT EACH PHASE READS AND WRITES on the shared `Landing`. The state is mutable and threaded
through every phase, so this table is the contract no signature states:

| Phase | Reads | Writes |
|---|---|---|
| `merge.arm_merge` | `opts.subject` | -- |
| `merge.await_merge` | `opts.merge_timeout`, `opts.merge_poll` | -- |
| `classify.resolve` | `opts.pr` | `merge_sha`, `ledger.t_merged`, `ledger.merge_sha` |
| `classify.classify` | `merge_sha`, `opts.since`, `opts.primary` | `resolved_tags`, `plane`, `self_applied`, `remaining_setup`, `needs_diff` |
| `classify.shortcut_if_nothing` | `resolved_tags`, `plane`, `self_applied`, `needs_diff` | -- |
| `ci.preflight` | `opts.primary` | -- |
| `ci.wait_master_ci` | `merge_sha`, `opts.ci_timeout` | `ledger.t_ci` |
| `tick.run_tick` | `opts.lock_retries`, `opts.lock_backoff` | `ledger.lock_waited`, `ledger.lock_holder`, `ledger.t_tick`, `tick_watch_abandoned` |
| `deploy.deploy_phase` | `resolved_tags`, `needs_diff`, `merge_sha`, `tick_watch_abandoned` | `resolved_tags`, `deployed_hosts`, `deployed_at`, `ledger.tags_label`, `ledger.cause`, `ledger.t_ci`, `ledger.t_tick`, `ledger.t_deploy` |
| `health_verdict.health` | `resolved_tags`, `deployed_at`, `plane`, `self_applied`, `remaining_setup`, `tick_watch_abandoned` | `ledger.cause` |

Every phase may end the landing by raising an `Outcome`, and the last one always does.
"""

from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import ci, classify, deploy, health_verdict, merge, tick
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say


def _step_resolve(ln: Landing) -> None:
    """Step 1 and 1½: the merge commit, then what this PR reaches."""
    classify.resolve(ln)
    classify.classify(ln)
    classify.shortcut_if_nothing(ln)


def _step_ci(ln: Landing) -> None:
    """Wait for master CI on the merge commit, and stamp when it answered."""
    ci.wait_master_ci(ln, ln.merge_sha)
    ln.ledger.t_ci = ln.tools.clock()


def _step_tick(ln: Landing) -> None:
    """Run the GitOps tick and stamp when it finished — or skip it here and stamp nothing.

    A PR reaching service tags ONLY deploys its own merge commit in step 5 (`deploy.sh --at`),
    so it needs the tick only to converge the primary checkout eventually. Step 5 kicks it
    once the deploy has returned, because the tick holds the tree lock for its whole unit run
    and deploy.sh would otherwise queue behind it. Nothing is awaited here and `tick=0` on the
    board says so. Eleven landings in the 14 days to 2026-09-11 spent the full 540s in this
    step watching a deployer busy with somebody else's apply.

    Every other shape keeps waiting, because there the tick is the apply: a PR with no service
    tag, whose verdict `no_tag_outcome` reads off the deployer's markers, and a PR that
    reaches tags AND something the tick applies itself, which the DECIDED note below covers.
    """
    # DECIDED: the fast path is for a PR that reaches SERVICE TAGS ONLY. A PR reaching tags AND
    # something the tick applies itself -- `tick_is_the_apply`, which is `self_applied` or
    # `remaining_setup` -- awaits the tick here exactly as it did before the fast path existed,
    # and step 5 deploys from the primary checkout with no `--at`.
    #
    # Because for that half the TICK is the apply, and step 6 grades it from the deployer's own
    # markers (`tick_state`, `broad_applied_covers`). Kicking a tick and reading those markers
    # seconds later grades a tick that has not run: `broad_applied` still holds an older origin
    # SHA, so `broad_applied_covers` is False and the landing prints `needs-manual-apply` with
    # a hand-run remedy the kicked tick performs a minute later -- or `deferred` (exit 75) when
    # a concurrent tick has left `behind_since` set. Neither is recoverable by re-reading: the
    # verdict has already been printed and the exit code returned.
    #
    # The alternative -- keep the fast path and await the kicked tick inside step 6 -- buys the
    # deploy's seconds back and costs the landing a second place that knows about tick timing,
    # in the phase whose whole job is to report. The latency this gives up is bounded to the
    # PRs that touch a service role and a setup role together.
    if ln.resolved_tags and not ln.tick_is_the_apply:
        say(
            "this landing deploys its own merge commit, so the tick is not awaited; step 5 "
            "kicks it after the deploy to converge the primary checkout"
        )
        ln.ledger.t_tick = ln.ledger.t_ci
        return
    tick.run_tick(ln)
    ln.ledger.t_tick = ln.tools.clock()


# The numbered steps, in order. The label is formatted with `pr=`; the number and the
# denominator both come from this list, so `== 3/6` cannot outlive a seventh step.
# The callables are bound HERE at import: a test that monkeypatches `ci.preflight` on its
# module does not reach the pipeline. Patch an entry of `_STEPS` instead.
_STEPS: tuple[tuple[str, Callable[[Landing], None]], ...] = (
    ("resolving PR #{pr}", _step_resolve),
    ("pre-flight: can the tick cross what is incoming?", ci.preflight),
    ("waiting for master CI", _step_ci),
    ("GitOps tick (fetch, ff-merge, deploy what is eligible)", _step_tick),
    ("deploying what the tick deferred", deploy.deploy_phase),
    ("health verdict", health_verdict.health),
)

STEP_COUNT = len(_STEPS)


def run(ln: Landing) -> Outcome:
    """Run every phase in order; the Outcome that ended it."""
    try:
        _phases(ln)
    except Outcome as outcome:
        ln.ledger.verdict = outcome.verdict or ""
        return outcome
    # Unreachable: the last step is `health_verdict.health`, which is `NoReturn`. Returned
    # rather than raised, because `run`'s own `except Outcome` above would not catch an
    # Outcome raised here and land.py would see a traceback instead of an exit code. It
    # carries a verdict so the `VERDICT:` line still prints and the ledger is not `aborted`.
    ln.ledger.verdict = Verdict.BLOCKED
    return Outcome(
        1,
        "the last phase must end the landing",
        verdict=Verdict.BLOCKED,
        error="pipeline fell through",
    )


def _phases(ln: Landing) -> None:
    # Every later phase runs git and deploy.sh with the primary checkout as cwd. A missing
    # one is worth one named line here rather than an unreadable failure five phases in;
    # land.sh checked it before anything else too.
    if not ln.opts.primary.is_dir():
        ln.die(f"cannot cd to {ln.opts.primary}", 1)
    if ln.opts.arm_merge:
        print(f"== arm  arming PR #{ln.opts.pr}'s merge")
        merge.arm_merge(ln)
    if ln.opts.await_merge:
        print(
            f"== 0/{STEP_COUNT}  waiting for PR #{ln.opts.pr} to merge "
            "(auto-merge or the merge queue)"
        )
        merge.await_merge(ln)
    for number, (label, phase) in enumerate(_STEPS, 1):
        print(f"== {number}/{STEP_COUNT}  {label.format(pr=ln.opts.pr)}")
        phase(ln)
