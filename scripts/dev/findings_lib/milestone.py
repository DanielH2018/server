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

A PROJECT is a session milestone that also holds a backlog. `milestone start --project`
writes the second description form, and `is_project` reads it. `milestone add` files
unclaimed issues under a project and `milestone remove` takes them out. `release`, `reap` and
`manual` leave an issue in a project, so a batch that fails returns its issue to the
backlog. An open project reserves its backlog for its branch: `reservations` is the one
answer to who may take an issue, which `next`, `list`, `new-issues` and `claim` read.
Everything else above holds for both forms. `held_issues` is the `--milestone` filter of
`next`, `list` and `new-issues`.

The tools own only a milestone whose description `branch_of` can parse. An operator's own
milestone is never reopened, closed or emptied.

Every call is a `gh api` REST call on `repos/<repo>/milestones`, because gh has no milestone
subcommand. The repo is in the path, since `gh api` takes no `--repo`.

Typical usage example:

    start("Opt-in session milestones", "worktree-a", "DanielH2018/server", dry_run=False)
"""

import re
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.


from dev.findings_lib.issue_model import Issue
from lib.gh import gh as real_gh, gh_json as real_gh_json
from lib.json_types import JsonValue, as_list, as_object, as_object_list

_DESCRIPTION_RE = re.compile(
    r"^Claude (session|project) milestone \(findings\.py\) for branch `([^`]+)`\.$"
)

# `--milestone none`: the issues no open milestone holds.
NO_MILESTONE = "none"

GhJson = Callable[..., JsonValue]
Gh = Callable[..., subprocess.CompletedProcess[str]]


class UnknownMilestone(ValueError):
    """`--milestone` named a title no milestone in the repo has."""


def description_for(branch: str, *, project: bool = False) -> str:
    """The description that marks a milestone as ``branch``'s session or project milestone."""
    form = "project" if project else "session"
    return f"Claude {form} milestone (findings.py) for branch `{branch}`."


def _owned(milestone: object) -> re.Match[str] | None:
    if not isinstance(milestone, dict):
        return None
    return _DESCRIPTION_RE.match(str(milestone.get("description") or ""))


def branch_of(milestone: object) -> str | None:
    """The branch owning ``milestone``, or None when these tools did not make it."""
    match = _owned(milestone)
    return match.group(2) if match else None


def is_project(milestone: object) -> bool:
    """Whether ``milestone`` is a project: a session milestone that holds a backlog."""
    match = _owned(milestone)
    return match is not None and match.group(1) == "project"


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
    project: bool = False,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> None:
    """Opens ``branch``'s milestone titled ``title``, as a project when ``project``.

    Starting the title the branch already has open does nothing. A closed milestone of
    this branch and title is reopened.

    Raises:
        ValueError: the branch already has an open milestone of another title, another
            branch or the operator has a milestone of this title, or the branch's
            milestone of this title has the other form. GitHub titles are unique per repo.
        subprocess.CalledProcessError: a gh call failed.
    """
    # DECIDED: only `start` makes a milestone, so a session without focused work gets none;
    # claim and PR attach to an existing one and never create (operator, 2026-11-15).
    current = for_branch(branch, repo, gh_json=gh_json)
    if current and current["state"] == "open":
        if current["title"] == title:
            _refuse_other_form(current, project)
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
                _refuse_other_form(m, project)
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
        f"description={description_for(branch, project=project)}",
    ]
    if dry_run:
        print("gh " + " ".join(argv))
    else:
        gh(*argv)


def _refuse_other_form(milestone: dict, project: bool) -> None:
    # DECIDED: no command converts a milestone between the two forms. A session milestone
    # made a project would hold claims its owner expects `release` to take out, and a
    # project made a session milestone would keep a backlog nothing lists (2026-11-19).
    if is_project(milestone) != project:
        form = "project" if is_project(milestone) else "session"
        raise ValueError(
            f"`{milestone['title']}` is a {form} milestone; `milestone close` it and "
            "start the other form under a new title"
        )


def held_issues(
    issues: list[Issue],
    title: str | None,
    repo: str,
    *,
    gh_json: GhJson = real_gh_json,
) -> list[Issue]:
    """The members of ``issues`` the milestone titled ``title`` holds.

    Args:
        issues: register issues, each with its `milestone` object or None.
        title: a milestone's title; `NO_MILESTONE` for the issues no open milestone
            holds, which includes one left in a closed milestone; None for every issue,
            which reads nothing.
        repo: OWNER/NAME.
        gh_json: the read boundary, for tests.

    Raises:
        UnknownMilestone: no milestone, open or closed, has this title. An empty answer
            would read as a project with nothing to pick up.
    """
    if title is None:
        return issues
    if title == NO_MILESTONE:
        numbers = {m["number"] for m in _milestones(repo, "open", gh_json)}
        return [i for i in issues if (i.milestone or {}).get("number") not in numbers]
    titled(title, repo, gh_json=gh_json)
    return [i for i in issues if i.milestone_title == title]


def titled(title: str, repo: str, *, gh_json: GhJson = real_gh_json) -> dict:
    """The milestone titled ``title``, open before closed.

    Raises:
        UnknownMilestone: no milestone, open or closed, has this title.
    """
    for state in ("open", "closed"):
        for m in _milestones(repo, state, gh_json):
            if m["title"] == title:
                return m
    raise UnknownMilestone(f"no milestone is titled `{title}`")


@dataclass(frozen=True)
class Counts:
    """How many issues and pull requests a milestone holds, by state.

    GitHub's own `open_issues` and `closed_issues` add the pull requests to the issues.
    """

    issues_open: int
    issues_closed: int
    prs_open: int
    prs_closed: int


def counts(number: object, repo: str, *, gh_json: GhJson = real_gh_json) -> Counts:
    """Counts what milestone ``number`` holds, its issues apart from its pull requests."""
    pages = gh_json(
        "api",
        f"repos/{repo}/issues?milestone={number}&state=all&per_page=100",
        "--paginate",
        "--slurp",
    )
    tally = Counter(
        ("prs" if "pull_request" in item else "issues", item["state"])
        for page in ([] if pages is None else as_list(pages, "milestone item pages"))
        for item in as_object_list(page, "milestone items")
    )
    return Counts(
        issues_open=tally["issues", "open"],
        issues_closed=tally["issues", "closed"],
        prs_open=tally["prs", "open"],
        prs_closed=tally["prs", "closed"],
    )


def reservations(
    issues: list[Issue],
    repo: str,
    *,
    branch: str | None = None,
    title: str | None = None,
    gh_json: GhJson = real_gh_json,
) -> dict[int, Mapping[str, object]]:
    """The open project milestone that reserves each of ``issues``, by issue number.

    An open project's backlog belongs to the project's branch: only that branch, and a
    fan-out batch claiming under it, may take an issue from it. This is the one answer to
    "who may take this issue" that `next`, `list`, `new-issues` and `claim` read. A closed
    project reserves nothing, and neither does a session milestone, which holds only what
    its branch already claimed.

    Args:
        issues: register issues, each with its `milestone` object or None.
        repo: OWNER/NAME.
        branch: the branch asking. Its own project reserves nothing against it.
        title: the `--milestone` a view reads. That project reserves nothing against it.
        gh_json: the read boundary, for tests. It is called only when an issue sits in
            another branch's project, because gh's issue JSON omits a milestone's state.

    Returns:
        ``issue.milestone`` for each reserved issue; an issue free to the asker is absent.
    """
    theirs = [
        i
        for i in issues
        if is_project(i.milestone)
        and branch_of(i.milestone) != branch
        and i.milestone_title != title
    ]
    if not theirs:
        return {}
    open_now = {m["number"] for m in _milestones(repo, "open", gh_json)}
    return {
        i.number: i.milestone
        for i in theirs
        if i.milestone and i.milestone.get("number") in open_now
    }


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
