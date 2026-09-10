"""Step 6: the health verdict, and the two halves a healthy deploy can still leave open.

The gate is the half `ansible-playbook` exiting 0 cannot speak to: readiness flips a
Deployment to Available before a bad liveness probe starts killing it. It is asked with
--no-post semantics -- the verdict returns to the session, not to Discord, where the
--detach path already reports.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing, TickState
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    Cause,
    Verdict,
    say,
)


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, tags = ln.opts.pr, ln.merge_sha, ln.tags_csv
    settled, lines = ln.tools.gate(ln.resolved_tags)
    for line in lines:
        say(line)
    if ln.plane:
        print(f"  STILL UNAPPLIED, and no deploy tag covers it: {ln.plane}")
    if not settled:
        ln.finish(Verdict.UNHEALTHY, 1, f"PR #{pr}, {sha}, tags: {tags}")
    if ln.plane:
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — services deployed, the plane above not",
        )
    # Only when the tick applies part of this PR itself does its state speak to THIS
    # landing; for an ordinary service PR, behind_since is somebody else's merge.
    if ln.self_applied:
        state = ln.tick_state()
        if state == TickState.UNKNOWN:
            print(
                "  services deployed, but the deployer's state directory "
                f"({ln.opts.deployer_state}) could not be read, so whether the tick "
                "applied its own half is unknown"
            )
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha}, tags: {tags} — services deployed, the tick's half unknown",
            )
        if state == TickState.HELD:
            print(
                f"  services deployed, but the deployer is holding {ln.state('hold_sha')}: "
                "its own apply failed — see hold_plane"
            )
            ln.ledger.cause = Cause.TICK_HELD
            ln.finish(
                Verdict.DEPLOY_FAILED,
                1,
                f"PR #{pr}, {sha} — services deployed, the tick's apply is held",
            )
        if state == TickState.BEHIND:
            print(
                "  services deployed, but the tick has not fast-forwarded to origin "
                f"(parked since: {ln.state('behind_since')})"
            )
            if ln.tick_watch_abandoned:
                print(ABANDONED_WATCH_NOTE)
                ln.finish(
                    Verdict.DEFERRED,
                    75,
                    f"PR #{pr}, {sha}, tags: {tags} — services deployed; this run stopped "
                    "watching a tick still applying, so the tick's half is unsettled",
                )
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr}, {sha}, tags: {tags} — services deployed, the tick's half not yet",
            )
        # CONVERGED says local == origin, which any session's `git merge --ff-only` produces
        # too — and after it holds the tick returns `noop` forever, so a plane it never applied
        # is stranded while this reads `settled` (issue #1537).
        if not ln.broad_applied_covers(sha):
            print(
                "  services deployed, but the tick recorded no broad apply covering this PR "
                f"(broad_applied: {ln.state('broad_applied') or 'absent'}) — something other "
                "than the tick fast-forwarded the checkout"
            )
            if ln.self_applied_command:
                print(f"  Apply it: {ln.self_applied_command}")
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha}, tags: {tags} — the tick converged without applying this PR",
            )
    if ln.remaining_setup:
        local = ln.tools.hostname()
        print(
            f"  services deployed and the tick applied on {local}, but it also reaches: "
            f"{ln.remaining_setup}"
        )
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha}, tags: {tags} — self-applied on {local} only; "
            "other hosts still need it",
        )
    ln.finish(Verdict.SETTLED, 0, f"PR #{pr}, {sha}, tags: {tags}")
