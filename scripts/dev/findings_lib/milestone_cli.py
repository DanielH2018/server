"""The `milestone` subcommand: open, show and close a branch's milestone, and fill a backlog.

`start "<title>"` opens the milestone and files the work the branch already holds under it:
its open claims and its open PR. A session that opts in after claiming then gets a milestone
that holds its own work. `start --project` opens a project milestone instead, and adopts a
group or an abandoned project of that title. `group "<title>" <n...>` makes a group, which
no branch owns, and files issues in it. `disown` returns the branch's project to a group.
`status` prints the milestone, its issue counts and, apart from them, its PR counts. `close`
abandons it early; the workflow and `reap` close a finished session or project milestone on
their own. `add <n...>` files unclaimed issues in a backlog and `remove <n...>` takes them
out. `status`, `close`, `add` and `remove` name the milestone by `--worktree <branch>`, or a
group by `--group "<title>"`. `findings_lib/milestone.py` owns the scheme.
"""

import argparse
import sys
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.


from lib.exit_codes import FINDINGS_BAD_ARGS
from lib.json_types import as_object_list
from dev.findings_lib import milestone
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.claim import branch_is_held
from dev.findings_lib.gh_calls import _load_issue, load_issues, run
from dev.findings_lib.issue_model import Issue
from dev.findings_lib.plans import (
    ClaimRefused,
    plan_milestone_add,
    plan_milestone_remove,
)


def open_issues(args: argparse.Namespace, tools: FindingsTools) -> list[Issue]:
    """The open register issues, cut to the ones ``args.milestone`` holds when it is set.

    Raises:
        SystemExit: with 64, after naming the title on stderr, when no milestone has it.
    """
    try:
        return milestone.held_issues(
            load_issues("open", tools),
            args.milestone,
            tools.owner_name,
            gh_json=tools.gh_json,
        )
    except milestone.UnknownMilestone as exc:
        sys.stderr.write(f"{args.cmd}: --milestone: {exc}\n")
        raise SystemExit(FINDINGS_BAD_ARGS) from None


def _backlog_milestone(args: argparse.Namespace, tools: FindingsTools) -> dict:
    """The open milestone whose backlog `add` and `remove` change.

    That is the group ``args.group`` titles, or the project of ``args.worktree``.

    Raises:
        ValueError: no such open group or project, in words that name the next command.
    """
    repo = tools.owner_name
    if args.group:
        found = milestone.group_titled(args.group, repo, gh_json=tools.gh_json)
        if found["state"] != "open":
            raise ValueError(
                f'`{args.group}` is closed; `milestone group "{args.group}"` reopens it'
            )
        return found
    project = milestone.for_branch(args.worktree, repo, gh_json=tools.gh_json)
    if not (project and project["state"] == "open" and milestone.is_project(project)):
        raise ValueError(
            f"`{args.worktree}` has no open project milestone; "
            '`milestone start --project "<title>"` opens one'
        )
    return project


def _change_backlog(
    verb: str, numbers: list[str], target: dict, dry_run: bool, tools: FindingsTools
) -> int:
    """Files ``numbers`` in the milestone ``target``, or takes them out, by ``verb``.

    Changes every issue it can and refuses the rest, as `claim` does.

    Returns 0 when every issue was changed or already as asked, 3 when any was refused.
    """
    plan = plan_milestone_add if verb == "add" else plan_milestone_remove
    changed, refused = 0, []
    for number in map(int, numbers):
        try:
            plans = plan(_load_issue(number, tools), target)
        except ClaimRefused as exc:
            refused.append(f"#{number} refused: {exc.reason}")
            continue
        run(plans, dry_run, tools)
        changed += bool(plans)
    done = {"add": "added to", "remove": "removed from"}[verb]
    unchanged = len(numbers) - changed - len(refused)
    print(
        f"{changed} {'would be ' if dry_run else ''}{done} `{target['title']}`"
        + (f", {unchanged} already there" if unchanged else "")
        + (f", {len(refused)} refused" if refused else "")
    )
    for line in refused:
        print(line, file=sys.stderr)
    return 3 if refused else 0


def _status(args: argparse.Namespace, tools: FindingsTools) -> int:
    repo = tools.owner_name
    if args.group:
        m = milestone.group_titled(args.group, repo, gh_json=tools.gh_json)
    else:
        m = milestone.for_branch(args.worktree, repo, gh_json=tools.gh_json)
        if m is None:
            print(
                f"`{args.worktree}` has no milestone; "
                '`milestone start "<title>"` opens one'
            )
            return 0
    form = (
        " project"
        if milestone.is_project(m)
        else " group"
        if milestone.is_group(m)
        else ""
    )
    held = milestone.counts(m["number"], repo, gh_json=tools.gh_json)
    print(
        f"`{m['title']}` {m['state']}{form}: "
        f"issues {held.issues_open} open, {held.issues_closed} closed; "
        f"PRs {held.prs_open} open, {held.prs_closed} closed  {m['html_url']}"
    )
    return 0


def _owner_is_held(tools: FindingsTools) -> Callable[[str], bool]:
    """`milestone.start`'s ``held``: `reap`'s rule for a branch something still holds."""

    def held(branch: str) -> bool:
        trees, dirty, merged, ok = tools.worktree_facts()
        return not ok or branch_is_held(
            branch, trees, dirty, merged, tools.peer_trees, tools.tree_held
        )

    return held


# Which of `--worktree` and `--group` names the milestone each action works on.
_NAMED_BY = {
    "start": {"worktree"},
    "disown": {"worktree"},
    "group": set(),
    "status": {"worktree", "group"},
    "close": {"worktree", "group"},
    "add": {"worktree", "group"},
    "remove": {"worktree", "group"},
}


def _bad_args(args: argparse.Namespace) -> str | None:
    """Why ``args`` cannot run, or None. Each line names the form that can."""
    verb = args.action
    if args.project and verb != "start":
        return "--project goes with `start`"
    given = {name for name in ("worktree", "group") if getattr(args, name)}
    takes = _NAMED_BY[verb]
    if len(given) != min(len(takes), 1) or not given <= takes:
        flags = " or ".join(f"--{name}" for name in sorted(takes, reverse=True))
        return f"`{verb}` takes {flags or 'neither --worktree nor --group'}"
    numbers = args.subject[verb == "group" :]
    if verb in ("start", "group") and not args.subject:
        return f'{verb}: give a title, `{verb} "<title>"`'
    if verb == "start" and len(args.subject) != 1:
        return 'start: give one title, `start "<title>"`'
    if verb in ("add", "remove") and not numbers:
        return f"{verb}: give issue numbers, `{verb} 1234 1235`"
    if verb in ("add", "remove", "group") and not all(s.isdigit() for s in numbers):
        return f"{verb}: give issue numbers, `{verb} 1234 1235`"
    return None


def cmd_milestone(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Runs `milestone start|group|status|close|disown|add|remove`.

    Returns 0 when done, 3 when the action was refused or found nothing to work on, 64 on
    arguments the action does not take.
    """
    bad = _bad_args(args)
    if bad:
        sys.stderr.write(f"milestone: {bad}\n")
        return FINDINGS_BAD_ARGS
    try:
        return _run(args, tools)
    except ValueError as exc:
        print(f"milestone {args.action} refused: {exc}", file=sys.stderr)
        return 3


def _run(args: argparse.Namespace, tools: FindingsTools) -> int:
    repo, branch, verb = tools.owner_name, args.worktree, args.action
    io = {"gh_json": tools.gh_json, "gh": tools.gh}
    if verb in ("add", "remove"):
        target = _backlog_milestone(args, tools)
        return _change_backlog(verb, args.subject, target, args.dry_run, tools)
    if verb == "group":
        title, *numbers = args.subject
        milestone.group(title, repo, dry_run=args.dry_run, **io)
        return _change_backlog("add", numbers, {"title": title}, args.dry_run, tools)
    if verb == "status":
        return _status(args, tools)
    if verb == "close" and args.group:
        milestone.close_group(args.group, repo, dry_run=args.dry_run, **io)
        print(f"closed `{args.group}`")
        return 0
    if verb == "close":
        title = milestone.close(branch, repo, dry_run=args.dry_run, **io)
        if title is None:
            raise ValueError(f"`{branch}` has no open milestone")
        print(f"closed `{title}`")
        return 0
    if verb == "disown":
        title = milestone.disown(branch, repo, dry_run=args.dry_run, **io)
        if title is None:
            raise ValueError(f"`{branch}` has no open project milestone")
        print(
            f"`{title}` is a group; "
            f'`milestone start --project "{title}" --worktree <branch>` adopts it'
        )
        return 0
    title = args.subject[0]
    adopted = milestone.start(
        title,
        branch,
        repo,
        dry_run=args.dry_run,
        project=args.project,
        held=_owner_is_held(tools),
        **io,
    )
    claimed = [i.number for i in load_issues("open", tools) if i.holder == branch]
    prs = as_object_list(
        tools.gh_json(
            "pr", "list", "--head", branch, "--state", "open", "--json", "number"
        ),
        "open PRs",
    )
    run(
        [["issue", "edit", str(n), "--milestone", title] for n in claimed]
        + [["pr", "edit", str(p["number"]), "--milestone", title] for p in prs],
        args.dry_run,
        tools,
    )
    would = "would " if args.dry_run else ""
    print(
        f"{'project ' if args.project else ''}milestone `{title}` "
        f"{'would open' if args.dry_run else 'open'} for `{branch}`; "
        f"{would}file {len(claimed)} claimed issue(s) and {len(prs)} PR(s) under it"
        + (
            f"; {'would adopt' if args.dry_run else 'adopted'} {adopted}"
            if adopted
            else ""
        )
    )
    return 0
