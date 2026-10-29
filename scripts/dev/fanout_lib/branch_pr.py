"""Which PR is a batch's own: the one GitHub holds for its branch, never the first URL in its text.

A final text names every PR the agent mentions. Batch 6309's body linked #6449, the landing it
had measured, before its own #6523, so the first URL in the text sent the review, the ready flip
and the `done` line to #6449 (#6551). The branch is the only key that cannot name another
batch's PR, so `own_pr` takes the PR list for the branch and uses the text only to choose among
them.

A text that names a PR while GitHub holds none for the branch is a defect to report, not a PR
to review: `OwnPr.problem` says so, and both callers end `needs input:` on it.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

# `.claude/hooks/fanout-stop.py` copies this pattern (the hooks stay stdlib-only) and
# `test_fanout_stop.py` holds the copies equal. `status.PR_URL` re-exports it.
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
BRANCH_PR_LIMIT = "10"


def branch_pr_argv(branch: str, repo: str) -> list[str]:
    """The `gh` command that lists every PR, in any state, opened from `branch` in `repo`."""
    return [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--json",
        "url,number",
        "--limit",
        BRANCH_PR_LIMIT,
    ]


def parse_branch_prs(stdout: str) -> list[str] | None:
    """The PR urls in `gh pr list --json url,number` output, newest first; None if unreadable."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    try:
        rows = sorted(data, key=lambda row: -int(row["number"]))
        return [str(row["url"]) for row in rows]
    except KeyError, TypeError, ValueError:
        return None


@dataclass(frozen=True)
class OwnPr:
    """The batch's own PR, or why the text's PR could not be it.

    Attributes:
        url: the PR GitHub holds for the branch, "" when there is none to use.
        problem: why a PR the text names is not the branch's, "" when nothing conflicts.
            Set only when `url` is empty.
    """

    url: str = ""
    problem: str = ""


def own_pr(text: str, branch: str, branch_prs: Sequence[str] | None) -> OwnPr:
    """Resolve the batch's PR from `text` against the PRs GitHub holds for `branch`.

    Args:
        text: the implementer's final text.
        branch: the batch's branch, named in the problem.
        branch_prs: the branch's PR urls, newest first. None when GitHub could not be asked.

    Returns:
        The branch's PR when it has one: the one the text names when it names one of them, else
        the newest. An empty `OwnPr` when the text names no PR at all, which the caller reads as
        `no-pr`. A `problem` when the text names PRs and the branch has none.
    """
    named = list(dict.fromkeys(PR_URL.findall(text)))
    if not named:
        return OwnPr()
    if branch_prs:
        return OwnPr(url=next((u for u in named if u in branch_prs), branch_prs[0]))
    why = (
        f"GitHub could not be asked for branch {branch}'s PR"
        if branch_prs is None
        else f"branch {branch} has no PR on GitHub"
    )
    return OwnPr(problem=f"the final text names {', '.join(named)}, but {why}")
