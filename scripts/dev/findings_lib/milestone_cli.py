"""The `milestone` subcommand: open, show and close a branch's milestone, and fill a project's.

`start "<title>"` opens the milestone and files the work the branch already holds under it:
its open claims and its open PR. A session that opts in after claiming then gets a milestone
that holds its own work. `start --project` opens a project milestone instead. `status` prints
the milestone and its counts. `close` abandons it early; the workflow and `reap` close a
finished one on their own. `add <n...>` files unclaimed issues in the branch's project
milestone, and `remove <n...>` takes them out. `findings_lib/milestone.py` owns the scheme.
"""

import argparse
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.


from lib.exit_codes import FINDINGS_BAD_ARGS
from lib.json_types import as_object_list
from dev.findings_lib import milestone
from dev.findings_lib.boundaries import FindingsTools
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


def _change_backlog(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Runs `milestone add|remove <n...>` on the project milestone of ``args.worktree``.

    Changes every issue it can and refuses the rest, as `claim` does.

    Returns 0 when every issue was changed or already as asked, 3 when the branch has no
    open project milestone or any issue was refused, 64 on a subject that is no number.
    """
    verb = args.action
    if not args.subject or not all(s.isdigit() for s in args.subject):
        sys.stderr.write(f"milestone {verb}: give issue numbers, `{verb} 1234 1235`\n")
        return FINDINGS_BAD_ARGS
    branch = args.worktree
    project = milestone.for_branch(branch, tools.owner_name, gh_json=tools.gh_json)
    if not (project and project["state"] == "open" and milestone.is_project(project)):
        print(
            f"milestone {verb} refused: `{branch}` has no open project milestone; "
            '`milestone start --project "<title>"` opens one',
            file=sys.stderr,
        )
        return 3
    plan = plan_milestone_add if verb == "add" else plan_milestone_remove
    changed, refused = 0, []
    for number in map(int, args.subject):
        try:
            plans = plan(_load_issue(number, tools), project)
        except ClaimRefused as exc:
            refused.append(f"#{number} refused: {exc.reason}")
            continue
        run(plans, args.dry_run, tools)
        changed += bool(plans)
    done = {"add": "added to", "remove": "removed from"}[verb]
    unchanged = len(args.subject) - changed - len(refused)
    print(
        f"{changed} {'would be ' if args.dry_run else ''}{done} `{project['title']}`"
        + (f", {unchanged} already there" if unchanged else "")
        + (f", {len(refused)} refused" if refused else "")
    )
    for line in refused:
        print(line, file=sys.stderr)
    return 3 if refused else 0


def cmd_milestone(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Runs `milestone start|status|close|add|remove` for ``args.worktree``.

    Returns 0 when done, 3 when `start`, `add` or `remove` was refused or `close` found
    nothing open.
    """
    if args.project and args.action != "start":
        sys.stderr.write("milestone: --project goes with `start`\n")
        return FINDINGS_BAD_ARGS
    repo, branch = tools.owner_name, args.worktree
    io = {"gh_json": tools.gh_json, "gh": tools.gh}
    if args.action in ("add", "remove"):
        return _change_backlog(args, tools)
    if args.action == "status":
        m = milestone.for_branch(branch, repo, gh_json=tools.gh_json)
        if m is None:
            print(f'`{branch}` has no milestone; `milestone start "<title>"` opens one')
            return 0
        print(
            f"`{m['title']}` {m['state']}{' project' if milestone.is_project(m) else ''}: "
            f"{m['open_issues']} open, {m['closed_issues']} closed  {m['html_url']}"
        )
        return 0
    if args.action == "close":
        title = milestone.close(branch, repo, dry_run=args.dry_run, **io)
        if title is None:
            print(f"`{branch}` has no open milestone", file=sys.stderr)
            return 3
        print(f"closed `{title}`")
        return 0
    if len(args.subject) != 1:
        sys.stderr.write('milestone start: give one title, `start "<title>"`\n')
        return FINDINGS_BAD_ARGS
    title = args.subject[0]
    try:
        milestone.start(
            title, branch, repo, dry_run=args.dry_run, project=args.project, **io
        )
    except ValueError as exc:
        print(f"milestone start refused: {exc}", file=sys.stderr)
        return 3
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
    print(
        f"{'project ' if args.project else ''}milestone `{title}` open for `{branch}`; "
        f"filed {len(claimed)} claimed issue(s) and {len(prs)} PR(s) under it"
    )
    return 0
