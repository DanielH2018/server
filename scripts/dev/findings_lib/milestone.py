"""The GitHub milestone that groups one piece of focused work by a Claude session.

A session opts in with `findings.py milestone start "<title>" --worktree <branch>`. The title
is free text, so the milestone names the work. The branch goes in the description, in the
form `description_for` writes, and that is how every later step finds the milestone. Until a
session runs `start`, nothing makes a milestone for it.

Once it exists, `findings.py claim` adds each issue the branch claims, and `release`, `reap`
and `manual` take it back out. `close` leaves a fixed issue in, because it is finished work.
The `session-milestone` workflow adds the branch's PR (`scripts/dev/session_milestone.py`).
A PR that closes an issue in some session's milestone joins that milestone instead, which is
how a fan-out batch's PR joins its orchestrator's milestone: the batch claimed the issue
under the orchestrator's branch. A claim or PR on a branch whose milestone has closed
reopens it.

The workflow closes a milestone when a PR or issue in it closes and nothing in it is left
open. `reap` closes one whose branch no worktree on either host holds any more, and
`milestone close` abandons one by hand.

The tools own only a milestone whose description `branch_of` can parse. An operator's own
milestone is never reopened, closed or emptied.

Every call is a `gh api` REST call on `repos/<repo>/milestones`, because gh has no milestone
subcommand. The repo is in the path, since `gh api` takes no `--repo`.

Typical usage example:

    start("Opt-in session milestones", "worktree-a", "DanielH2018/server", dry_run=False)
"""

import re
import subprocess
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.gh import gh as real_gh, gh_json as real_gh_json
from lib.json_types import JsonValue, as_list, as_object, as_object_list

_DESCRIPTION_RE = re.compile(
    r"^Claude session milestone \(findings\.py\) for branch `([^`]+)`\.$"
)

GhJson = Callable[..., JsonValue]
Gh = Callable[..., subprocess.CompletedProcess[str]]


def description_for(branch: str) -> str:
    """The description that marks a milestone as ``branch``'s session milestone."""
    return f"Claude session milestone (findings.py) for branch `{branch}`."


def branch_of(milestone: object) -> str | None:
    """The branch owning ``milestone``, or None when these tools did not make it."""
    if not isinstance(milestone, dict):
        return None
    match = _DESCRIPTION_RE.match(str(milestone.get("description") or ""))
    return match.group(1) if match else None


def _milestones(repo: str, state: str, gh_json: GhJson) -> list[dict]:
    pages = gh_json(
        "api",
        f"repos/{repo}/milestones?state={state}&per_page=100",
        "--paginate",
        "--slurp",
    )
    return [
        m
        for page in ([] if pages is None else as_list(pages, "milestone pages"))
        for m in as_object_list(page, "milestones")
    ]


def _set_state(repo: str, number: object, state: str, dry_run: bool, gh: Gh) -> None:
    argv = [
        "api",
        "-X",
        "PATCH",
        f"repos/{repo}/milestones/{number}",
        "-f",
        f"state={state}",
    ]
    if dry_run:
        print("gh " + " ".join(argv))
    else:
        gh(*argv)


def for_branch(
    branch: str, repo: str, *, gh_json: GhJson = real_gh_json
) -> dict | None:
    """``branch``'s session milestone, open before closed, or None when it has none.

    Of several closed ones, the newest wins. The closed list is read only when no open
    milestone matches, and it grows by one per milestone `start` makes.
    """
    for state in ("open", "closed"):
        mine = [m for m in _milestones(repo, state, gh_json) if branch_of(m) == branch]
        if mine:
            return max(mine, key=lambda m: m["number"])
    return None


def attach_target(
    branch: str,
    repo: str,
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> str | None:
    """The title to file ``branch``'s work under, reopening its milestone if closed.

    Returns None when the branch never ran `start`, which is the case for most sessions.
    """
    milestone = for_branch(branch, repo, gh_json=gh_json)
    if milestone is None:
        return None
    if milestone["state"] == "closed":
        _set_state(repo, milestone["number"], "open", dry_run, gh)
    return milestone["title"]


def start(
    title: str,
    branch: str,
    repo: str,
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> None:
    """Opens ``branch``'s session milestone titled ``title``.

    Starting the title the branch already has open does nothing. A closed milestone of
    this branch and title is reopened.

    Raises:
        ValueError: the branch already has an open milestone of another title, or another
            branch or the operator has a milestone of this title. GitHub titles are
            unique per repo.
        subprocess.CalledProcessError: a gh call failed.
    """
    # DECIDED: only `start` makes a milestone, so a session without focused work gets none;
    # claim and PR attach to an existing one and never create (operator, 2026-11-15).
    current = for_branch(branch, repo, gh_json=gh_json)
    if current and current["state"] == "open":
        if current["title"] == title:
            return
        raise ValueError(
            f"`{branch}` already has the open milestone `{current['title']}`; "
            "`milestone close` it first"
        )
    for state in ("open", "closed"):
        for m in _milestones(repo, state, gh_json):
            if m["title"] != title:
                continue
            owner = branch_of(m)
            if owner == branch:
                _set_state(repo, m["number"], "open", dry_run, gh)
                return
            raise ValueError(
                f"milestone `{title}` already exists, "
                + (f"for `{owner}`" if owner else "and is not a session milestone")
            )
    argv = [
        "api",
        "-X",
        "POST",
        f"repos/{repo}/milestones",
        "-f",
        f"title={title}",
        "-f",
        f"description={description_for(branch)}",
    ]
    if dry_run:
        print("gh " + " ".join(argv))
    else:
        gh(*argv)


def close(
    branch: str,
    repo: str,
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> str | None:
    """Closes ``branch``'s open milestone, returning its title, or None if none is open."""
    milestone = for_branch(branch, repo, gh_json=gh_json)
    if milestone is None or milestone["state"] != "open":
        return None
    _set_state(repo, milestone["number"], "closed", dry_run, gh)
    return milestone["title"]


def close_if_done(
    number: int,
    repo: str,
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> bool:
    """Closes milestone ``number`` when it is a session milestone with nothing open in it.

    Returns whether it closed, or a dry run would. GitHub's `open_issues` counts open PRs
    too, and can lag the close that triggered this call; `reap` closes what that misses.
    """
    m = as_object(
        gh_json("api", f"repos/{repo}/milestones/{number}"), f"milestone {number}"
    )
    # DECIDED: close at zero open items, accepting that multi-PR work closes between PRs and
    # reopens on the next claim or PR (operator, 2026-11-15).
    if branch_of(m) is None or m["state"] != "open" or m["open_issues"]:
        return False
    _set_state(repo, number, "closed", dry_run, gh)
    return True


def close_idle(
    repo: str,
    held: Callable[[str], bool],
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> list[str]:
    """Closes each open session milestone in ``repo`` whose branch nothing is working.

    Args:
        repo: OWNER/NAME.
        held: takes a branch, returns whether a worktree on either host still holds it, or
            whether that cannot be known. A milestone it calls held stays open.
        dry_run: print the writes instead of making them.
        gh_json: the read boundary, for tests.
        gh: the write boundary, for tests.

    Returns:
        The titles closed, or that a dry run would close.
    """
    closed = []
    for m in _milestones(repo, "open", gh_json):
        branch = branch_of(m)
        if branch is None or m["open_issues"] or held(branch):
            continue
        _set_state(repo, m["number"], "closed", dry_run, gh)
        closed.append(m["title"])
    return closed


def pr_branch(pr: dict, issue_milestone: Callable[[int], dict | None]) -> str:
    """The branch whose milestone a pull request belongs in.

    The first issue the PR closes that sits in a session milestone decides it. Otherwise
    it is the PR's own head branch.

    Args:
        pr: `gh pr view --json headRefName,closingIssuesReferences`.
        issue_milestone: takes an issue number, returns its `milestone` object or None.
    """
    for ref in pr.get("closingIssuesReferences") or []:
        branch = branch_of(issue_milestone(ref["number"]))
        if branch:
            return branch
    return pr["headRefName"]
