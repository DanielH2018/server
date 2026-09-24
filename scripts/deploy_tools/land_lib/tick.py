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
from deploy_tools.exit_codes import (
    TICK_JOINED,
    TICK_LOCK_CONTENTION,
    TICK_OK,
    TICK_STILL_RUNNING,
)
from deploy_tools.land_lib.landing import Landing, retry_while_locked
from deploy_tools.land_lib.outcome import Cause, Verdict, say


def kick_tick(ln: Landing) -> None:
    """Start a tick and return without watching it; never ends the landing.

    For a landing that deploys its PR's merge commit itself (`deploy.sh --at`). Nothing it
    does next needs the primary checkout to be at that commit, so the tick is started only so
    the checkout converges for whoever reads it later. What became of the request is booked
    as `ledger.kick`, because the board is the only place the answer outlives the log.

    DECIDED: called AFTER `deploy.sh` returns, not before it. `gitops-deploy.service` wraps
    its whole unit run in the git-tree lock, and deploy.sh waits up to LOCK_WAIT (3300s) for
    that same lock to cut its snapshot -- so a tick kicked first does not remove the wait, it
    moves it out of `tick=` and into `lock=`. Kicked after, it runs while this landing gates,
    and the gate's own snapshot takes no lock at all.

    DECIDED: a non-zero exit here is LOGGED AND IGNORED, where `run_tick` below ends the
    landing on one. The two are asked different questions. `run_tick` is the step that APPLIES
    the change, so a tick that did not run means nothing was deployed; this kick applies
    nothing, and the deployer's own 10-minute timer converges the checkout whether this
    request landed or not. Failing a landing whose deploy succeeded, because a convenience
    request to systemd did not, would be the worse answer.

    A JOINED request (exit 4) started nothing: the run in flight fetched origin before this
    PR merged, so it does not carry the merge commit and the checkout stays behind when it
    ends. Until 2026-09-17 that exit was 0 and read as converging (issue #1843) -- in exactly
    the case a landing meets most often, a deployer mid-tick. Booked as `joined` here;
    `rearm_tick` below asks again once the gate has run.

    No `observe` callback: `--no-wait` returns as soon as systemd has the request, so there is
    no wait for the wrapper to report and `lock` has nothing to book.
    """
    rc = ln.tools.tick(wait=False)
    if rc == TICK_OK:
        ln.ledger.kick = "started"
        say("tick kicked, not awaited (this landing deployed the merge commit itself)")
        return
    if rc == TICK_JOINED:
        ln.ledger.kick = "joined"
        say(
            "tick kick joined a run already in flight, which fetched before this PR merged; "
            "it is asked again after the health gate"
        )
        return
    ln.ledger.kick = "failed"
    say(
        f"tick kick failed (exit {rc}); carrying on -- the deploy above rendered the merge "
        "commit, and the deployer's own timer converges the primary checkout"
    )


def rearm_tick(ln: Landing) -> None:
    """Ask for the tick a second time, after the health gate, when the first request joined.

    DECIDED: a second `systemctl start` after the gate, rather than a deferred one. The polkit
    rule admits `start` on this one unit and nothing else, so a transient timer or a
    `systemd-run --on-active` is refused, and a request made while the joined run is still
    `activating` is coalesced into it again. The gate is the longest thing a landing does
    after the kick -- a rollout wait plus the 180s restart window -- so it is the one point
    where the joined run has most likely ended. A second join is booked and said, not
    retried: the deployer's own 10-minute timer converges the checkout, and this landing's
    deploy is already live and gated. No-op unless the first kick joined. Unreachable after a
    deploy that failed, because `deploy_outcome` ends the landing before the gate: a `joined`
    row beside a `deploy-failed` verdict was asked once, and convergence is moot when nothing
    shipped.
    """
    if ln.ledger.kick != "joined":
        return
    rc = ln.tools.tick(wait=False)
    if rc == TICK_OK:
        ln.ledger.kick = "rearmed"
        say("tick re-armed after the gate; the primary checkout converges from here")
        return
    say(
        f"tick re-arm did not start a run (exit {rc}); this landing did NOT converge the "
        "primary checkout -- the deployer's timer does, within 10 minutes"
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
