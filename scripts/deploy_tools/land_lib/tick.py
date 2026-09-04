"""Step 4, the GitOps tick, retried while the unit's own flock gives up.

Exit 3 means the tick fast-forwarded NOTHING. A landing that carried on from there left
the primary checkout behind origin with every later step reading that as "the tick
deferred" (#723, 2026-09-01). Each attempt already waits 180s inside the unit, so five of
them cover a long deploy. This is the one implementation both call sites use -- step 4 and
the stale retry in the deploy phase. land.sh's stale-retry copy had none of the retry or
the accounting (#1013).
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say


def run_tick(ln: Landing) -> None:
    """Run the tick; 0 and 75 continue, exhausted contention is lock-busy, else die."""
    o, t = ln.opts, ln.tools
    rc = 0
    for attempt in range(1, o.lock_retries + 1):
        # Sampled BEFORE the attempt: read afterwards, the holder has usually released and
        # the landing books an empty one (issue #1031).
        # DECIDED: this samples on every attempt, including an uncontended first one, where
        # bash's `note_lock_contention` only ran fuser+ps after an attempt had already lost
        # the lock. That is a real parity delta (#1085 item 4) and it stays: reverting to
        # bash's post-failure sample reintroduces the #1031 race this pre-sample exists to
        # close, and `test_the_lock_holder_is_sampled_before_the_attempt`
        # (tests/test_land_tick.py) plus its deploy.py sibling would go red on the revert.
        # The cost is two short-lived processes per attempt against a 10-15 minute landing.
        holder = t.lock_holder()
        started = t.clock()
        rc = t.tick()
        if rc != 3:
            break
        ln.note_lock_contention(int(t.clock() - started) + o.lock_backoff, holder)
        say(
            f"tick skipped for lock contention (attempt {attempt}/{o.lock_retries}); retrying in {o.lock_backoff}s"
        )
        t.sleep(o.lock_backoff)
    # 75 = the wrapper stopped watching a run still in flight. Not a failure, and it leaves
    # the ff-merge either done or retryable next tick.
    if rc in (0, 75):
        say(f"tick exit {rc}")
        return
    if rc == 3:
        ln.die(
            f"tick skipped for lock contention {o.lock_retries} times — nothing fast-forwarded",
            75,
            "lock-busy",
        )
    # gitops_tick.sh exit 1 is "the unit exited non-zero" -- the one verdict-less path that
    # maps to a recurring operational event, so it names deploy-failed rather than landing in
    # the board's `aborted` bucket (issue #1031).
    ln.ledger.cause = "tick-failed"
    ln.die(f"gitops tick failed (exit {rc})", 1, "deploy-failed")
