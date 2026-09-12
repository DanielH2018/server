"""Step 4, the GitOps tick, retried while the unit's own flock gives up.

Exit 3 means the tick fast-forwarded NOTHING. A landing that carried on from there left
the primary checkout behind origin with every later step reading that as "the tick
deferred" (#723, 2026-09-01). Each attempt already waits 180s inside the unit, so five of
them cover a long deploy. This is the one implementation both call sites use -- step 4 and
the stale retry in the deploy phase. land.sh's stale-retry copy had none of the retry or
the accounting (#1013).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.exit_codes import TICK_LOCK_CONTENTION, TICK_OK, TICK_STILL_RUNNING
from deploy_tools.land_lib.landing import Landing, retry_while_locked
from deploy_tools.land_lib.outcome import Cause, Verdict, say


def kick_tick(ln: Landing) -> None:
    """Start a tick and return without watching it; never ends the landing.

    For a landing that deploys its PR's merge commit itself (`deploy.sh --at`). Nothing it
    does next needs the primary checkout to be at that commit, so the tick is started only so
    the checkout converges for whoever reads it later.

    DECIDED: a non-zero exit here is LOGGED AND IGNORED, where `run_tick` below ends the
    landing on one. The two are asked different questions. `run_tick` is the step that APPLIES
    the change, so a tick that did not run means nothing was deployed; this kick applies
    nothing, and the deployer's own 10-minute timer converges the checkout whether this
    request landed or not. Failing a landing whose deploy succeeded, because a convenience
    request to systemd did not, would be the worse answer.

    No `observe` callback: `--no-wait` returns as soon as systemd has the request, so there is
    no wait for the wrapper to report and `lock` has nothing to book.
    """
    rc = ln.tools.tick(wait=False)
    if rc == TICK_OK:
        say("tick kicked, not awaited (this landing deploys the merge commit itself)")
        return
    say(
        f"tick kick failed (exit {rc}); carrying on -- the deploy below renders the merge "
        "commit, and the deployer's own timer converges the primary checkout"
    )


def run_tick(ln: Landing) -> None:
    """Run the tick; 0 and 75 continue, exhausted contention is lock-busy, else die."""
    o, t = ln.opts, ln.tools
    rc = retry_while_locked(
        ln,
        TICK_LOCK_CONTENTION,
        lambda: t.tick(observe=ln.note_in_flock_wait),
        lambda n: (
            f"tick skipped for lock contention (attempt {n}/{o.lock_retries}); retrying in {o.lock_backoff}s"
        ),
    )
    # 75 = the wrapper stopped watching a run still in flight. Not a failure, and it leaves
    # the ff-merge either done or retryable next tick.
    if rc in (TICK_OK, TICK_STILL_RUNNING):
        say(f"tick exit {rc}")
        # Booked because every later read of the deployer's markers then races the apply this
        # landing gave up watching: `hold_sha` is legitimately empty while the apply is still
        # running, which is indistinguishable from a settled deferral. Issue #1607 -- a landing
        # reported `deferred` during the very apply that failed and parked the whole fleet.
        #
        # ASSIGNED, not raised: this is the second call site (the deploy phase's stale retry
        # runs the tick again), and a retry that returns 0 watched a tick to completion. Left
        # sticky, that landing would print the race note over markers that are now settled.
        ln.tick_watch_abandoned = rc == TICK_STILL_RUNNING
        return
    if rc == TICK_LOCK_CONTENTION:
        ln.die(
            f"tick skipped for lock contention {o.lock_retries} times — nothing fast-forwarded",
            75,
            Verdict.LOCK_BUSY,
        )
    # gitops_tick.sh exit 1 is "the unit exited non-zero" -- the one verdict-less path that
    # maps to a recurring operational event, so it names deploy-failed rather than landing in
    # the board's `aborted` bucket (issue #1031).
    ln.ledger.cause = Cause.TICK_FAILED
    ln.die(f"gitops tick failed (exit {rc})", 1, Verdict.DEPLOY_FAILED)
