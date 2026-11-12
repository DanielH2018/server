"""A check run GitHub leaves pending under a completed check suite, which no wait outlasts (#7996).

PR #7975's image-smoke job "pull + boot changed images" stayed `queued` with no runner after
its workflow run read `completed` / `failure`. The master CI gate requires that check, so GitHub
refused every direct merge, and the landing polled for 75 minutes towards merge-timeout. Two
remedies failed: `gh run rerun` refuses a completed run ("This workflow run cannot be
retried"), and a `workflow_dispatch` run on the branch does not replace the PR's check. Only a
new head commit gives the PR a fresh set of checks.

`find` names such checks on one head with two reads: the head's check runs, then its check
suites, one suite per workflow run. `merge.await_merge` calls it only on a poll where await_ci
reads the head green and the wait has sat on that head for `AFTER_S`, so the ordinary path
makes no extra read. A read that fails names nothing, so it never ends a wait on its own.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import as_list, as_object_list
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import Landing

# How long a green head waits unmerged before its checks are read, and between reads after.
AFTER_S = 600

_PENDING_RUNS = (
    '[.check_runs[] | select(.status != "completed") | {name, suite: .check_suite.id}]'
)
_COMPLETED_SUITES = '[.check_suites[] | select(.status == "completed") | .id]'


def find(ln: Landing, head: str) -> list[str]:
    """The names of `head`'s check runs still pending under a completed suite; [] on a failed read."""

    def pending() -> list:
        url = f"repos/{{owner}}/{{repo}}/commits/{head}/check-runs?per_page=100"
        got = ln.tools.gh_json("api", url, "--jq", _PENDING_RUNS)
        return as_object_list(got, "gh api check-runs")

    def completed() -> list:
        url = f"repos/{{owner}}/{{repo}}/commits/{head}/check-suites?per_page=100"
        got = ln.tools.gh_json("api", url, "--jq", _COMPLETED_SUITES)
        return as_list(got, "gh api check-suites")

    try:
        runs = gh_read.read(pending, ln.tools.sleep, "gh api check-runs")
        if not runs:
            return []
        done = set(gh_read.read(completed, ln.tools.sleep, "gh api check-suites"))
    except gh_read.GhReadFailed, ValueError:
        return []
    return sorted({str(r.get("name")) for r in runs if r.get("suite") in done})


def dead_end(pr: str, head: str, names: list[str]) -> str:
    """Why the PR cannot merge at `head`, and the one remedy that clears it."""
    checks = ", ".join(f"'{n}'" for n in names)
    return (
        f"PR #{pr}'s check {checks} on {head[:8]} is still pending, but its workflow run "
        "completed, so GitHub never finishes it; a re-run is refused and a workflow_dispatch "
        "does not replace it. Push an empty commit to the PR branch "
        "(`git commit --allow-empty -m 'Re-run CI' && git push`), then re-run this"
    )
