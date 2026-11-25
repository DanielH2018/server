"""The `next` subcommand: the issues a session may pick up, and a count of what it withholds.

`findings_lib/claim.py:free_issues` decides the free set. This file prints it, with one
line for the issues a date or a blocker withholds and one for the issues an open project
reserves, so no withheld issue leaves the view without a count.
"""

import argparse
import json
import sys


from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.claim import free_issues
from dev.findings_lib.gh_calls import open_pr_refs
from dev.findings_lib.issue_model import today_utc, waiting, waiting_line
from dev.findings_lib.milestone_cli import open_issues
from lib.progress import count_line


def waiting_summary(rows: list[dict]) -> list[str]:
    """The one line `next` prints in place of ``rows``' `waiting_line`s, none for no rows."""
    if not rows:
        return []
    kinds = count_line("blocked" if r["blocked_by"] else "deferred" for r in rows)
    return [f"{len(rows)} waiting ({kinds}); --waiting to list"]


def reserved_summary(rows: list[dict]) -> list[str]:
    """The one line `next` prints for the ``rows`` open projects reserve, none for no rows."""
    if not rows:
        return []
    projects = count_line(f"in `{r['project']}`" for r in rows)
    return [
        f'{len(rows)} reserved by a project ({projects}); --milestone "<title>" to list'
    ]


def cmd_next(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``next`` subcommand: prints the issues a session may pick up, best first.

    `claim.free_issues` decides the free set. On a failed worktree read it withholds every
    claimed issue rather than refusing like `reap`: `next` never writes, so it degrades to
    the more conservative read instead of refusing to answer at all.

    Args:
        args: parsed CLI namespace carrying ``limit``, ``json``, ``milestone`` and
            ``waiting``.
        tools: the process boundaries every gh call goes through.
    """
    issues = open_issues(args, tools)
    today = today_utc()
    rows, stale, reserved = free_issues(
        issues,
        tools,
        pr_refs=open_pr_refs(tools),
        today=today,
        milestone=args.milestone,
    )
    rows = rows[: args.limit]
    # Under `--json` the note goes to stderr: a waiting row inside the array would be claimed.
    held_rows = waiting(issues, today=today, tracked=tools.tracked())
    held_back = (
        [waiting_line(r, today) for r in held_rows]
        if args.waiting
        else waiting_summary(held_rows)
    ) + reserved_summary(reserved)
    if args.json:
        print(json.dumps(rows))
        for line in held_back:
            sys.stderr.write(line + "\n")
        return 0
    for line in held_back:
        print(line)
    for r in rows:
        held = (
            f"  [stale claim by `{stale[r['number']]}`]" if r["number"] in stale else ""
        )
        # Offered, not withheld: a solo session works it. Only a fan-out batch may not.
        solo = "  [solo-only: cites fan-out tooling]" if r["solo_only"] else ""
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['domain'] or '-':<21} "
            f"{r['title']}{held}{solo}"
        )
    if any(r["number"] in stale for r in rows):
        print(
            "note: a stale claim is released by `findings.py reap`; `claim` also reaps one "
            "before taking the issue"
        )
    if not rows:
        print("nothing to pick up")
    return 0
