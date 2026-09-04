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


def run_tick(ln: Landing) -> None:
    """Run the tick; 0 and 75 continue, exhausted contention is lock-busy, else die."""
    o, t = ln.opts, ln.tools
    rc = retry_while_locked(
        ln,
        TICK_LOCK_CONTENTION,
        t.tick,
        lambda n: (
            f"tick skipped for lock contention (attempt {n}/{o.lock_retries}); retrying in {o.lock_backoff}s"
        ),
    )
    # 75 = the wrapper stopped watching a run still in flight. Not a failure, and it leaves
    # the ff-merge either done or retryable next tick.
    if rc in (TICK_OK, TICK_STILL_RUNNING):
        say(f"tick exit {rc}")
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
