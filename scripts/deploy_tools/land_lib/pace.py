"""Merge pacing: hold a green PR's merge while master CI already has a backlog (#5526).

Every merge to master starts a full `ci.yml` sweep of about 17 jobs, unless the `prove` job
finds the pushed tree already tested. The account runs at most 20 GitHub-hosted jobs at once.
With eight concurrent landings on 2026-10-24, six master runs sat unfinished together, their
jobs waited 8-17 minutes for a runner, and a master run took 29-61 minutes from creation to
verdict. Every landing waits on that verdict, so merging faster than master CI clears only
deepens the queue.

`hold` reads the unfinished master push runs of `ci.yml` before `await_merge` merges a green
head. At or above the cap it returns the line to print, and the merge waits for the next poll.
The count is a read, not a lock: two landings that read at the same poll can both see room and
both merge. Pacing lowers the average queue depth; it does not close the race that makes the
`prove` job's tested-tree skip miss.

A held poll does not count against `merge_timeout`, so a landing queued behind other landings
waits for its turn rather than ending at `merge-timeout` while the queue drains (#6466).
Instead, the cap rises by one for each `RAISE_CAP_EVERY_S` the landing has been held, so a
landing that keeps losing the race still merges in bounded time. A failed read merges: pacing
is a throughput control, not a safety gate.

`LAND_PACE_MASTER_RUNS` sets the cap, and `0` turns pacing off. A PR armed by hand with
`gh pr merge --auto` merges without passing through here.
"""

import subprocess
from datetime import datetime

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import as_list, as_object
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say

# DECIDED: an unfinished run created more than 2 hours ago is not counted. On 2026-10-24 the
# slowest master run took 61 minutes from creation to finish, so a run still unfinished at
# twice that is wedged rather than queued, and counting it would hold every landing until
# GitHub expires its jobs after 24 hours.
STUCK_AFTER_S = 2 * 60 * 60
# DECIDED: a held landing's cap rises by one per 30 minutes held, rather than pacing ending
# at a fixed budget. On 2026-10-28 eight landers raced for one slot and each lost often enough
# to run out its 2700s `merge_timeout`, so five ended `merge-timeout` and needed a hand re-run
# (#6466). Turning pacing off once a budget ran out would release every lander that arrived
# together at once, the pile #5526 measured. Raising the cap lets the longest-held landers
# merge one at a time first. 30 minutes is about one master run under load (29-61 minutes on
# 2026-10-24), and a lander still merges well inside the units' 8-hour LAND_DEADLINE.
RAISE_CAP_EVERY_S = 30 * 60
# Enough to see every run inside the 2-hour window at today's merge rate.
_RUNS_URL = (
    "repos/{owner}/{repo}/actions/workflows/ci.yml/runs"
    f"?branch={BRANCH}&event=push&per_page=50"
)


def unfinished_master_runs(ln: Landing) -> int:
    """How many master push runs of `ci.yml` are not completed and not wedged.

    Raises:
        subprocess.CalledProcessError: gh failed.
        subprocess.TimeoutExpired: gh timed out.
        ValueError: gh's answer did not parse.
    """
    # Every status but `completed` counts. The API's `status=` filter takes one value, and a run
    # whose last job is waiting reads `queued` even after most of its jobs finished.
    got = ln.tools.gh_json(
        "api",
        _RUNS_URL,
        "--jq",
        "[.workflow_runs[] | {status, created_at}]",
    )
    now = ln.tools.wall_clock()
    count = 0
    for item in as_list(got, "gh api ci.yml runs"):
        run = as_object(item, "a ci.yml run")
        if run.get("status") == "completed":
            continue
        created = datetime.fromisoformat(
            str(run.get("created_at")).replace("Z", "+00:00")
        )
        if now - created.timestamp() <= STUCK_AFTER_S:
            count += 1
    return count


def hold(ln: Landing, held_s: int) -> str:
    """Why this poll does not merge; '' to merge now.

    Prints a line of its own only when the read fails, because the caller prints the hold.

    Args:
        ln: The landing.
        held_s: The seconds this landing's earlier polls were held, which raise the cap.
    """
    base = ln.opts.pace_master_runs
    if base <= 0:
        return ""
    raised = held_s // RAISE_CAP_EVERY_S
    cap = base + raised
    try:
        backlog = unfinished_master_runs(ln)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        why = getattr(exc, "stderr", None) or str(exc)
        say(
            f"could not count unfinished master CI runs ({why.strip()}); merging without pacing"
        )
        return ""
    if backlog < cap:
        return ""
    limit = f"LAND_PACE_MASTER_RUNS={base}"
    if raised:
        limit = f"{cap} ({limit}, raised by {raised} after {raised * RAISE_CAP_EVERY_S}s held)"
    return (
        f"pacing the merge: {backlog} master CI run(s) unfinished, at or above "
        f"{limit}; merging once fewer are"
    )
