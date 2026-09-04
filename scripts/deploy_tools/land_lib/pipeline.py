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
| `classify.classify` | `merge_sha`, `opts.since` | `resolved_tags`, `plane`, `self_applied`, `remaining_setup`, `needs_diff` |
| `classify.shortcut_if_nothing` | `resolved_tags`, `plane`, `self_applied`, `needs_diff` | -- |
| `ci.preflight` | `opts.primary` | -- |
| `ci.wait_master_ci` | `merge_sha`, `opts.ci_timeout` | `ledger.t_ci` |
| `tick.run_tick` | `opts.lock_retries`, `opts.lock_backoff` | `ledger.lock_waited`, `ledger.lock_holder`, `ledger.t_tick` |
| `deploy.deploy_phase` | `resolved_tags`, `needs_diff`, `merge_sha` | `resolved_tags`, `deployed_hosts`, `ledger.tags_label`, `ledger.cause`, `ledger.t_deploy` |
| `health_verdict.health` | `resolved_tags`, `plane`, `self_applied`, `remaining_setup` | `ledger.cause` |

Every phase may end the landing by raising an `Outcome`, and the last one always does.
"""

from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import ci, classify, deploy, health_verdict, merge, tick
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict


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
    """Run the GitOps tick, and stamp when it finished."""
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
