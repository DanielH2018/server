"""Step 6: the health verdict, and the two halves a healthy deploy can still leave open.

The gate is the half `ansible-playbook` exiting 0 cannot speak to: readiness flips a
Deployment to Available before a bad liveness probe starts killing it. It is asked with
--no-post semantics -- the verdict returns to the session, not to Discord, where the
--detach path already reports.
"""

from __future__ import annotations

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, tags = ln.opts.pr, ln.merge_sha, ln.tags
    print("== 6/6  health verdict")
    settled, lines = ln.tools.gate([x for x in tags.split(",") if x])
    for line in lines:
        say(line)
    if ln.plane:
        print(f"  STILL UNAPPLIED, and no deploy tag covers it: {ln.plane}")
    if not settled:
        ln.finish("unhealthy", 1, f"PR #{pr}, {sha}, tags: {tags}")
    if ln.plane:
        ln.finish(
            "needs-manual-apply",
            1,
            f"PR #{pr}, {sha} — services deployed, the plane above not",
        )
    # Only when the tick applies part of this PR itself does its state speak to THIS
    # landing; for an ordinary service PR, behind_since is somebody else's merge.
    if ln.self_applied:
        state = ln.tick_state()
        if state == "held":
            print(
                f"  services deployed, but the deployer is holding {ln.state('hold_sha')}: "
                "its own apply failed — see hold_plane"
            )
            ln.finish(
                "deploy-failed",
                1,
                f"PR #{pr}, {sha} — services deployed, the tick's apply is held",
            )
        if state == "behind":
            print(
                "  services deployed, but the tick has not fast-forwarded to origin "
                f"(parked since: {ln.state('behind_since')})"
            )
            ln.finish(
                "deferred",
                75,
                f"PR #{pr}, {sha}, tags: {tags} — services deployed, the tick's half not yet",
            )
    ln.finish("settled", 0, f"PR #{pr}, {sha}, tags: {tags}")
