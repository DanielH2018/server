"""A check run GitHub leaves pending under a completed check suite, which no wait outlasts.

An image-smoke job such as "pull + boot changed images" can stay `queued` with no runner after
its workflow run reads `completed` / `failure`. The master CI gate requires that check, so GitHub
refuses every direct merge, and the landing polls towards merge-timeout. Two
remedies fail: `gh run rerun` refuses a completed run ("This workflow run cannot be
retried"), and a `workflow_dispatch` run on the branch does not replace the PR's check. Only a
new head commit gives the PR a fresh set of checks.

`find` names such checks on one head with two reads: the head's check runs, then its check
suites, one suite per workflow run. `merge.await_merge` calls it only on a poll where await_ci
reads the head green and the wait has sat on that head for `AFTER_S`, so the ordinary path
makes no extra read. A read that fails names nothing, so it never ends a wait on its own.
"""

from lib import host_modules  # noqa: F401
from lib.json_types import as_list

from ci_verdict import CheckRun, CheckSuite, parse_check_runs, parse_check_suites
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import Landing

# How long a green head waits unmerged before its checks are read, and between reads after.
AFTER_S = 600


def find(ln: Landing, head: str) -> list[str]:
    """The names of `head`'s check runs still pending under a completed suite; [] on a failed read."""

    def listing(name: str, key: str) -> list:
        url = f"repos/{{owner}}/{{repo}}/commits/{head}/{name}?per_page=100"
        return as_list(
            ln.tools.gh_json("api", url, "--jq", f".{key}"), f"gh api {name}"
        )

    def pending() -> list[CheckRun]:
        runs = parse_check_runs(listing("check-runs", "check_runs"))
        return [r for r in runs if r.status != "completed"]

    def completed() -> list[CheckSuite]:
        suites = parse_check_suites(listing("check-suites", "check_suites"))
        return [s for s in suites if s.completed]

    try:
        runs = gh_read.read(pending, ln.tools.sleep, "gh api check-runs")
        if not runs:
            return []
        suites = gh_read.read(completed, ln.tools.sleep, "gh api check-suites")
    except gh_read.GhReadFailed, ValueError:
        return []
    done = {s.id for s in suites if s.id is not None}
    return sorted({r.name for r in runs if r.suite_id in done})


def dead_end(pr: str, head: str, names: list[str]) -> str:
    """Why the PR cannot merge at `head`, and the one remedy that clears it."""
    checks = ", ".join(f"'{n}'" for n in names)
    return (
        f"PR #{pr}'s check {checks} on {head[:8]} is still pending, but its workflow run "
        "completed, so GitHub never finishes it; a re-run is refused and a workflow_dispatch "
        "does not replace it. Push an empty commit to the PR branch "
        "(`git commit --allow-empty -m 'Re-run CI' && git push`), then re-run this"
    )
