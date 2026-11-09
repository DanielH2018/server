"""Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose.

A _BROAD_MANUAL_PREFIXES change anywhere in the incoming range stops the tick
fast-forwarding however green CI turns out.
"""

import json
import subprocess
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import (
    CI_DISARMED,
    CI_GREEN,
    CI_PENDING,
    CI_RED,
    CI_UNPROVEN,
    DEPLOY_BROAD,
)
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Verdict, disarmed, say


def blockers(ln: Landing) -> subprocess.CompletedProcess[str]:
    """deploy_tags.py blockers, in the primary checkout; exit 0 clear, 3 blocked, else broken."""
    return ln.tools.deploy_tags(ln.opts.primary, ["blockers", f"origin/{BRANCH}"])


def blocked_detail(stdout: str) -> str:
    """What a blocked pre-flight's VERDICT names: the paths needing a hand and the command.

    cc-wait relays only the VERDICT line, so the detail carries what stderr said. Output that
    is not the blockers document still blocks, with a pointer to the command that says why.
    """
    try:
        doc = json.loads(stdout)
        paths, run = list(doc["needing_hand"]), str(doc["run"])
    except ValueError, KeyError, TypeError:
        return "an incoming change needs a hand; `deploy_cli.py tags blockers` names it"
    shown = ", ".join(paths[:2]) + (
        f" and {len(paths) - 2} more" if len(paths) > 2 else ""
    )
    return f"{shown} needs a hand; if it is your change, run: {run}"


def preflight(ln: Landing) -> None:
    """Step 2: can the tick cross what is incoming? Before waiting on anything."""
    res = blockers(ln)
    if res.returncode == 0:
        say("nothing in the way")
        return
    if res.returncode == DEPLOY_BROAD:
        ln.finish(
            Verdict.BLOCKED,
            1,
            f"PR #{ln.opts.pr} — {blocked_detail(res.stdout or '')}",
        )
    ln.die(
        f"pre-flight failed (exit {res.returncode}) — land.sh deployed nothing itself",
        1,
    )


def wait_master_ci(ln: Landing, sha: str) -> None:
    """Wait for master CI on `sha`; red and no-verdict each end the landing with a name."""
    rc, line = ln.tools.await_ci(sha, ln.opts.ci_timeout)
    print(line)
    if rc == CI_GREEN:
        return
    if rc == CI_RED:
        ln.die(
            f"master CI is RED on {sha} — land.sh deployed nothing itself",
            1,
            Verdict.CI_RED,
        )
    if rc == CI_PENDING:
        ln.die(
            f"no CI verdict inside {ln.opts.ci_timeout}s — land.sh deployed nothing itself",
            75,
            Verdict.CI_TIMEOUT,
        )
    if rc == CI_UNPROVEN:
        # Exit 1, not 75: 75 is the resume point, and no re-run converges here until the
        # operator dispatches CI or promotes the tip. `line` names the tip and the command.
        # An earlier promotion can already have carried the merge, so the message claims
        # only what this landing did.
        tags = f" (this PR's tags: {ln.tags_csv})" if ln.tags_csv else ""
        ln.die(
            f"no CI verdict can arrive: {line}{tags} — land.sh deployed nothing itself",
            1,
            Verdict.CI_UNPROVEN,
        )
    if rc == CI_DISARMED:
        ln.die(disarmed("", "land.sh deployed nothing itself"), 1)
    ln.die(f"await_ci failed (exit {rc}) — land.sh deployed nothing itself", 1)
