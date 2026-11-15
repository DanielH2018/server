"""The GitHub milestone that groups one Claude session's work.

A session is a worktree branch, `worktree-<name>`, and its milestone is titled with that
branch. `findings.py claim` adds each issue the session takes to the milestone, and `release`,
`reap` and `manual` take it back out. `close` leaves a fixed issue in, because it is the
session's finished work. The `pr-milestone` workflow adds the session's pull request
(`scripts/dev/pr_milestone.py`). A fan-out batch in this repo claims under its orchestrator's
branch, so its issues and its PR join the orchestrator's milestone.

`reap` closes a session milestone once no worktree on either host holds its branch and no
open issue or PR is left in it. A later claim or PR on the same branch reopens it.

The tools own only a milestone whose description is `DESCRIPTION`. An operator's own
milestone is never reopened, closed or emptied, even if its title starts `worktree-`.

Every call is a `gh api` REST call on `repos/<repo>/milestones`, because gh has no milestone
subcommand. The repo is in the path, since `gh api` takes no `--repo`.

Typical usage example:

    ensure("worktree-session-milestones", "DanielH2018/server", dry_run=False)
"""

import subprocess
from collections.abc import Callable
from typing import TypeGuard

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.gh import gh as real_gh, gh_json as real_gh_json
from lib.json_types import JsonValue, as_list, as_object_list

SESSION_PREFIX = "worktree-"
DESCRIPTION = "Claude session work for this worktree branch (findings.py)."

GhJson = Callable[..., JsonValue]
Gh = Callable[..., subprocess.CompletedProcess[str]]


def session_title(branch: str | None) -> str | None:
    """The milestone title for ``branch``, or None when it is not a session's branch."""
    return branch if branch and branch.startswith(SESSION_PREFIX) else None


def is_session_milestone(milestone: object) -> TypeGuard[dict]:
    """Whether ``milestone`` is one these tools made, by its description."""
    return isinstance(milestone, dict) and milestone.get("description") == DESCRIPTION


def pr_title(pr: dict, issue_milestone: Callable[[int], dict | None]) -> str | None:
    """The session milestone a pull request belongs in, or None.

    The first issue the PR closes that sits in a session milestone decides it. A fan-out
    batch's PR then joins its orchestrator's milestone, because the batch claimed the issue
    under the orchestrator's branch. A PR closing no such issue falls back to its own head
    branch.

    Args:
        pr: `gh pr view --json headRefName,closingIssuesReferences`.
        issue_milestone: takes an issue number, returns its `milestone` object or None.
    """
    for ref in pr.get("closingIssuesReferences") or []:
        milestone = issue_milestone(ref["number"])
        if is_session_milestone(milestone):
            return milestone["title"]
    return session_title(pr.get("headRefName"))


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


def ensure(
    title: str,
    repo: str,
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> None:
    """Makes sure an open session milestone titled ``title`` exists in ``repo``.

    Reads only the open milestones, then creates. The closed list grows by one per session,
    so it is read only when the create answers that the title exists, and the closed
    session milestone found there is reopened.

    Raises:
        ValueError: a milestone of that title exists that these tools do not own, so
            `gh issue edit --milestone` would file the session's work under it.
        subprocess.CalledProcessError: a gh call failed.
    """
    found = _titled(title, _milestones(repo, "open", gh_json))
    if found:
        return
    create = [
        "api",
        "-X",
        "POST",
        f"repos/{repo}/milestones",
        "-f",
        f"title={title}",
        "-f",
        f"description={DESCRIPTION}",
    ]
    if dry_run:
        print("gh " + " ".join(create))
        return
    try:
        gh(*create)
        return
    except subprocess.CalledProcessError as exc:
        if "already_exists" not in (exc.stderr or ""):
            raise
    found = _titled(title, _milestones(repo, "closed", gh_json))
    if not found:
        raise ValueError(
            f"milestone `{title}` was refused as existing but was not listed"
        )
    gh(
        "api",
        "-X",
        "PATCH",
        f"repos/{repo}/milestones/{found['number']}",
        "-f",
        "state=open",
    )


def _titled(title: str, milestones: list[dict]) -> dict | None:
    """The milestone titled ``title``, None if absent, refused if not a session one."""
    for m in milestones:
        if m["title"] == title:
            if not is_session_milestone(m):
                raise ValueError(
                    f"milestone `{title}` exists and is not a session milestone"
                )
            return m
    return None


def close_idle(
    repo: str,
    held: Callable[[str], bool],
    *,
    dry_run: bool,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> list[str]:
    """Closes each open session milestone in ``repo`` that nothing is working.

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
        if not is_session_milestone(m) or m["open_issues"] or held(m["title"]):
            continue
        argv = [
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/milestones/{m['number']}",
            "-f",
            "state=closed",
        ]
        if dry_run:
            print("gh " + " ".join(argv))
        else:
            gh(*argv)
        closed.append(m["title"])
    return closed
