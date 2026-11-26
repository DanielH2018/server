"""Which PR is a batch's own: the one GitHub holds for its branch, never the first URL in its text.

A final text names every PR the agent mentions, and the first URL can be a PR the agent only
measured, which would send the review, the ready flip and the `done` line to the wrong PR. The
branch is the only key that cannot name another batch's PR, so `own_pr` takes the PR list for the branch and uses the text only to choose among
them.

A text that names a PR while GitHub holds none for the branch is a defect to report, not a PR
to review: `OwnPr.problem` says so, and both callers end `needs input:` on it.

A read GitHub did not answer sets `problem` too, and `OwnPr.unanswered` with it. The pipeline
still ends `needs input:` there, because it reviews and lands no PR it cannot confirm.
`status` leaves the batch as its final text reads, because a failed read says nothing about
the batch.

A closed PR that never merged is no batch's own. `fanout.py place abandon` leaves the remote
branch and its closed PR behind, a relaunch reuses the branch name, and the closed PR's old CI
then read as the new batch's. `parse_branch_prs` drops such PRs and lists an open PR
before a merged one.

A branch match alone does not make a merged PR the batch's own. `launch` names the branch from
the batch id, so a relaunch of an issue whose PR merged reuses the name. A relaunched batch
that opened nothing can cite its branch's earlier merged PR, and the pipeline would land that
PR again. `own_pr` reads a citation of such a PR as no claim at all: the batch is
`no-pr`, not in conflict. The pipeline resolves its PR before anything has merged it, so it counts
only open PRs as its own. `status` runs after the landing too, so it sets aside only the PRs that
merged before the batch's `launched_at`.
"""

import json
from dataclasses import dataclass
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

# `.claude/hooks/fanout-stop.py` copies this pattern (the hooks stay stdlib-only);
# `test_fanout_stop.py` holds the copies equal.
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
        "url,number,state,mergedAt",
        "--limit",
        BRANCH_PR_LIMIT,
    ]


@dataclass(frozen=True)
class BranchPrs:
    """The PRs GitHub holds for one branch, newest first in each state.

    Attributes:
        open: the urls of its open PRs.
        merged: the urls of its merged PRs.
    """

    open: tuple[str, ...] = ()
    merged: tuple[str, ...] = ()

    @property
    def urls(self) -> list[str]:
        """Every url, open before merged."""
        return [*self.open, *self.merged]


def parse_branch_prs(stdout: str) -> BranchPrs | None:
    """The branch's open and merged PRs; None if unreadable.

    A closed PR that never merged is dropped. A row with no `state` reads as open,
    which `gh pr list --json state` never omits.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    try:
        rows = sorted(data, key=lambda row: -int(row["number"]))
        states = [
            (str(row.get("state", "OPEN")).upper(), str(row["url"])) for row in rows
        ]
        return BranchPrs(
            open=tuple(url for state, url in states if state == "OPEN"),
            merged=tuple(url for state, url in states if state == "MERGED"),
        )
    except KeyError, TypeError, ValueError, AttributeError:
        return None


def open_pr_argv(branch: str, repo: str) -> list[str]:
    """The `gh` command that reads the open PR opened from `branch` in `repo`, with its checks."""
    return [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "url,isDraft,statusCheckRollup",
        "--limit",
        "1",
    ]


# A cancelled run is no verdict on its head (`docs/landing.md`), so it is not listed as failing.
_FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
_FAILED_STATES = frozenset({"FAILURE", "ERROR"})


@dataclass(frozen=True)
class OpenPr:
    """A branch's open PR, which a launch of the same batch continues instead of starting over.

    Attributes:
        url: the PR's URL.
        draft: whether the PR is a draft.
        failing: one `<check name> <link>` line per check that failed on its head.
    """

    url: str
    draft: bool
    failing: tuple[str, ...] = ()


def parse_open_pr(stdout: str) -> OpenPr | None | str:
    """The open PR in `open_pr_argv`'s output, None when there is none, a reason when unreadable."""
    try:
        rows = json.loads(stdout)
        if not rows:
            return None
        row = rows[0]
        failing = []
        for check in row.get("statusCheckRollup") or []:
            if "context" in check:
                name, link, failed = (
                    check["context"],
                    check.get("targetUrl") or "",
                    str(check.get("state")).upper() in _FAILED_STATES,
                )
            else:
                name, link, failed = (
                    check["name"],
                    check.get("detailsUrl") or "",
                    str(check.get("conclusion")).upper() in _FAILED_CONCLUSIONS,
                )
            if failed:
                failing.append(f"{name} {link}".strip())
        return OpenPr(str(row["url"]), row.get("isDraft") is True, tuple(failing))
    except KeyError, TypeError, ValueError, AttributeError, IndexError:
        return f"`gh pr list` printed what could not be read: {stdout.strip()[:200]!r}"


def parse_merged_before(stdout: str, launched_at: str) -> list[str]:
    """The urls of the branch's PRs that merged before `launched_at`; [] if either is unreadable."""
    try:
        launched = datetime.fromisoformat(launched_at)
        return [
            str(row["url"])
            for row in json.loads(stdout)
            if row.get("mergedAt")
            and datetime.fromisoformat(row["mergedAt"]) < launched
        ]
    except KeyError, TypeError, ValueError, AttributeError:
        return []


@dataclass(frozen=True)
class OwnPr:
    """The batch's own PR, or why the text's PR could not be it.

    Attributes:
        url: the PR GitHub holds for the branch, "" when there is none to use.
        problem: why a PR the text names is not the branch's, "" when nothing conflicts.
            Set only when `url` is empty.
        unanswered: GitHub could not be asked, so `problem` describes the read, not the batch.
    """

    url: str = ""
    problem: str = ""
    unanswered: bool = False


def own_pr(
    text: str,
    branch: str,
    branch_prs: Sequence[str] | None,
    earlier: Sequence[str] = (),
) -> OwnPr:
    """Resolve the batch's PR from `text` against the PRs GitHub holds for `branch`.

    Args:
        text: the implementer's final text.
        branch: the batch's branch, named in the problem.
        branch_prs: the branch's open then merged PR urls from this launch, newest first. None
            when GitHub could not be asked.
        earlier: the branch's merged PR urls from an earlier launch of the same batch id.

    Returns:
        The branch's PR when it has one: the one the text names when it names one of them, else
        the newest. An empty `OwnPr` when the text names no PR, or only `earlier` ones, which
        the caller reads as `no-pr`. A `problem` when the text names any other PR and the
        branch has none, marked `unanswered` when `branch_prs` is None.
    """
    named = list(dict.fromkeys(PR_URL.findall(text)))
    if not named:
        return OwnPr()
    if branch_prs:
        return OwnPr(url=next((u for u in named if u in branch_prs), branch_prs[0]))
    named = [u for u in named if u not in earlier]
    if not named:
        return OwnPr()
    if branch_prs is None:
        why = f"GitHub could not be asked for branch {branch}'s PR"
    elif earlier:
        why = f"branch {branch} holds only {', '.join(earlier)}, from an earlier launch"
    else:
        why = f"branch {branch} has no open or merged PR on GitHub"
    return OwnPr(
        problem=f"the final text names {', '.join(named)}, but {why}",
        unanswered=branch_prs is None,
    )


def worktree_own_pr(
    text: str,
    worktree: Path,
    repo: str,
    run: Callable[[list[str], str | None], subprocess.CompletedProcess],
) -> OwnPr:
    """`own_pr` for the branch `worktree` has checked out, its PRs read through `run`.

    Asks GitHub only when the text names a PR, so a `no-pr` batch makes no `gh` call.
    """
    if not PR_URL.search(text):
        return OwnPr()
    head = run(["git", "-C", str(worktree), "symbolic-ref", "--short", "HEAD"], None)
    branch = head.stdout.strip()
    held = None
    if branch:
        proc = run(branch_pr_argv(branch, repo), None)
        if proc.returncode == 0:
            held = parse_branch_prs(proc.stdout)
    if held is None:
        return own_pr(text, branch or "(no branch)", None)
    return own_pr(text, branch, held.open, held.merged)


def needs_input_report(problem: str) -> dict:
    """The result a batch ends on when its text names a PR that is not its branch's."""
    return {
        "type": "result",
        "is_error": False,
        "result": f"needs input: {problem}, so nothing was reviewed or landed.",
    }
