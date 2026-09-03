"""Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose.

A _BROAD_MANUAL_PREFIXES change anywhere in the incoming range stops the tick
fast-forwarding, which guarantees deploy.sh refuses as stale (exit 4) however green CI
turns out. Landing PR #570 on 2026-08-29 spent ~6 minutes waiting for CI and then failed
at step 4, with the blocker visible in the range before the wait began.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say


def blockers(ln: Landing) -> int:
    """deploy_tags.py blockers, in the primary checkout; 0 clear, 3 blocked, else broken."""
    return ln.tools.deploy_tags(
        ln.opts.primary, ["blockers", f"origin/{BRANCH}"]
    ).returncode


def preflight(ln: Landing) -> None:
    """Step 2: can the tick cross what is incoming? Before waiting on anything."""
    print("== 2/6  pre-flight: can the tick cross what is incoming?")
    rc = blockers(ln)
    if rc == 0:
        say("nothing in the way")
        return
    if rc == 3:
        ln.finish(
            "blocked",
            1,
            f"PR #{ln.opts.pr} — an incoming change needs a hand; see above",
        )
    ln.die(f"pre-flight failed (exit {rc}) — nothing deployed", 1)


def wait_master_ci(ln: Landing, sha: str, label: str) -> None:
    """Wait for master CI on `sha`; red and no-verdict each end the landing with a name."""
    rc, line = ln.tools.await_ci(sha, ln.opts.ci_timeout)
    print(line)
    if rc == 0:
        return
    if rc == 1:
        ln.die(f"master CI is RED on {label} — nothing deployed", 1, "ci-red")
    if rc == 75:
        ln.die(
            f"no CI verdict on {label} inside {ln.opts.ci_timeout}s — nothing deployed",
            75,
            "ci-timeout",
        )
    ln.die(f"await_ci failed (exit {rc}) — nothing deployed", 1)
