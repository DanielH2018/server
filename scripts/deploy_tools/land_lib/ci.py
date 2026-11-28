"""Step 2, the master CI wait on the merge commit."""

from lib.exit_codes import (
    CI_DISARMED,
    CI_GREEN,
    CI_PENDING,
    CI_RED,
    CI_UNPROVEN,
)
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Cause, Verdict, disarmed
from deploy_tools.land_lib.transitions import CHECKS


def wait_master_ci(ln: Landing, sha: str) -> None:
    """Wait for master CI on `sha`; red and no-verdict each end the landing with a name."""
    rc, line = ln.tools.await_ci(
        sha, ln.opts.ci_timeout, ln.transitions.counter(CHECKS)
    )
    print(line)
    if rc == CI_GREEN:
        return
    if rc == CI_RED:
        why = f"master CI is RED on {sha}"
        if _turned_master_red(ln, sha):
            ln.ledger.cause = Cause.OWN_RED
            why += ", and green on its first parent, so this PR's change turned it red"
        ln.die(f"{why} — land.sh deployed nothing itself", 1, Verdict.CI_RED)
    if rc == CI_PENDING:
        ln.die(
            f"no CI verdict inside {ln.opts.ci_timeout}s — land.sh deployed nothing itself",
            75,
            Verdict.CI_TIMEOUT,
        )
    if rc == CI_UNPROVEN:
        # Exit 1, not 75: no re-run converges until the operator dispatches CI or promotes the tip.
        tags = f" (this PR's tags: {ln.tags_csv})" if ln.tags_csv else ""
        ln.die(
            f"no CI verdict can arrive: {line}{tags} — land.sh deployed nothing itself",
            1,
            Verdict.CI_UNPROVEN,
        )
    if rc == CI_DISARMED:
        ln.die(disarmed("", "land.sh deployed nothing itself"), 1)
    ln.die(f"await_ci failed (exit {rc}) — land.sh deployed nothing itself", 1)


def _turned_master_red(ln: Landing, sha: str) -> bool:
    """Whether master CI is red on the merge commit `sha` itself and green on its first parent.

    The wait's red may be a descendant's or the tip's, which another PR can have caused.
    """
    parent = ln.git("rev-parse", "--verify", f"{sha}^1")
    first = parent.stdout.strip()
    if parent.returncode or not first:
        return False
    return ln.tools.ci_passed(sha) is False and ln.tools.ci_passed(first) is True
