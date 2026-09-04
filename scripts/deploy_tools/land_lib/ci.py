"""Step 2, pre-flight, and step 3, the master CI wait -- in that order, on purpose.

A _BROAD_MANUAL_PREFIXES change anywhere in the incoming range stops the tick
fast-forwarding, which guarantees deploy.sh refuses as stale (exit 4) however green CI
turns out. Landing PR #570 on 2026-08-29 spent ~6 minutes waiting for CI and then failed
at step 4, with the blocker visible in the range before the wait began.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.exit_codes import CI_GREEN, CI_PENDING, CI_RED, DEPLOY_BROAD
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Verdict, say


def blockers(ln: Landing) -> int:
    """deploy_tags.py blockers, in the primary checkout; 0 clear, 3 blocked, else broken."""
    return ln.tools.deploy_tags(
        ln.opts.primary, ["blockers", f"origin/{BRANCH}"]
    ).returncode


def preflight(ln: Landing) -> None:
    """Step 2: can the tick cross what is incoming? Before waiting on anything."""
    rc = blockers(ln)
    if rc == 0:
        say("nothing in the way")
        return
    if rc == DEPLOY_BROAD:
        ln.finish(
            Verdict.BLOCKED,
            1,
            f"PR #{ln.opts.pr} — an incoming change needs a hand; see above",
        )
    ln.die(f"pre-flight failed (exit {rc}) — nothing deployed", 1)


def wait_master_ci(ln: Landing, sha: str, label: str | None = None) -> None:
    """Wait for master CI on `sha`; red and no-verdict each end the landing with a name.

    `label` names what is being waited on when it is not simply the merge commit -- the
    stale-retry caller passes `the tip <sha>`. Without one the messages read as step 3's
    always did: the sha for a red CI, and no `on ...` clause at all for a timeout.
    """
    rc, line = ln.tools.await_ci(sha, ln.opts.ci_timeout)
    print(line)
    if rc == CI_GREEN:
        return
    where = f" on {label}" if label else ""
    if rc == CI_RED:
        # bash said "on $MERGE_SHA — nothing deployed" for step 3's own wait, and "on the
        # tip $TIP_SHA — the tick cannot cross it; nothing deployed" for the stale retry --
        # a caller passing `label` is always the latter, which the tick genuinely cannot
        # cross once it names an origin tip that never went green.
        cannot_cross = " — the tick cannot cross it;" if label else " —"
        ln.die(
            f"master CI is RED on {label or sha}{cannot_cross} nothing deployed",
            1,
            Verdict.CI_RED,
        )
    if rc == CI_PENDING:
        ln.die(
            f"no CI verdict{where} inside {ln.opts.ci_timeout}s — nothing deployed",
            75,
            Verdict.CI_TIMEOUT,
        )
    ln.die(f"await_ci failed{where} (exit {rc}) — nothing deployed", 1)
