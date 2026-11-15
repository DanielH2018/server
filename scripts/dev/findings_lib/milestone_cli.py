"""The `milestone` subcommand: open, show and close a branch's session milestone.

`start "<title>"` opens the milestone and files the work the branch already holds under it:
its open claims and its open PR. A session that opts in after claiming then gets a milestone
that holds its own work. `status` prints the milestone and its counts. `close` abandons it
early; the workflow and `reap` close a finished one on their own. `findings_lib/milestone.py`
owns the scheme.
"""

import argparse
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.exit_codes import FINDINGS_BAD_ARGS
from lib.json_types import as_object_list
from dev.findings_lib import milestone
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import load_issues, run
from dev.findings_lib.issue_model import current_claim, validate_worktree_name


def cmd_milestone(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Runs `milestone start|status|close` for ``args.worktree``.

    Returns 0 when done, 3 when `start` was refused or `close` found nothing open.
    """
    bad = validate_worktree_name(args.worktree)
    if bad:
        sys.stderr.write(f"milestone: --worktree `{args.worktree}` {bad}\n")
        return FINDINGS_BAD_ARGS
    repo, branch = tools.owner_name, args.worktree
    io = {"gh_json": tools.gh_json, "gh": tools.gh}
    if args.action == "status":
        m = milestone.for_branch(branch, repo, gh_json=tools.gh_json)
        if m is None:
            print(f'`{branch}` has no milestone; `milestone start "<title>"` opens one')
            return 0
        print(
            f"`{m['title']}` {m['state']}: {m['open_issues']} open, "
            f"{m['closed_issues']} closed  {m['html_url']}"
        )
        return 0
    if args.action == "close":
        title = milestone.close(branch, repo, dry_run=args.dry_run, **io)
        if title is None:
            print(f"`{branch}` has no open milestone", file=sys.stderr)
            return 3
        print(f"closed `{title}`")
        return 0
    if not args.title:
        sys.stderr.write('milestone start: give the title, `start "<title>"`\n')
        return FINDINGS_BAD_ARGS
    try:
        milestone.start(args.title, branch, repo, dry_run=args.dry_run, **io)
    except ValueError as exc:
        print(f"milestone start refused: {exc}", file=sys.stderr)
        return 3
    claimed = [
        i["number"] for i in load_issues("open", tools) if current_claim(i) == branch
    ]
    prs = as_object_list(
        tools.gh_json(
            "pr", "list", "--head", branch, "--state", "open", "--json", "number"
        ),
        "open PRs",
    )
    run(
        [["issue", "edit", str(n), "--milestone", args.title] for n in claimed]
        + [["pr", "edit", str(p["number"]), "--milestone", args.title] for p in prs],
        args.dry_run,
        tools,
    )
    print(
        f"milestone `{args.title}` open for `{branch}`; filed {len(claimed)} claimed "
        f"issue(s) and {len(prs)} PR(s) under it"
    )
    return 0
