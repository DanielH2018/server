#!/usr/bin/env python3
"""Print the session branches the worktree pruner must not delete in this repo, one per line.

The pruner is the dotfiles repo's `~/.claude/hooks/prune-worktrees.py`. It deletes a session
branch no worktree holds once the branch's work is on origin/master, and it knows nothing about
this repo's registers. `.claude/prune-worktrees.json` names this script as its `branch_holds`
command, so the pruner runs it before it deletes a branch and keeps every branch printed here.

Two registers hold a branch:

- an open PR has it as its head, by one `gh pr list`;
- a claim in the findings register names it, by `findings.py claims --json`. Stale claims
  count too. `findings.py claims` exits 0 when its worktree read failed, and every claim then
  reads stale, so its `live` field cannot clear a branch for deletion. A stale claim holds its
  branch only until `findings.py reap` releases it.

Exit 1 means a register could not be read, and stderr names it. The pruner then keeps every
branch it would have deleted and says so, which is the point: a host whose `gh` stopped
answering must not delete a branch an open PR still needs.

Usage:
    uv run python scripts/dev/held_branches.py
"""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.gh import gh
from lib.repo_paths import REPO
from lib.worktrees import primary_checkout

OPEN_PR_LIMIT = 1000


def open_pr_heads(repo: str, limit: int = OPEN_PR_LIMIT) -> set[str] | None:
    """The head branch of every open PR, or None when the forge gave no whole answer.

    None covers a `gh` that failed and a list cut off at `limit`: neither may read as
    "no open PR".

    Args:
        repo: the checkout `gh` runs in.
        limit: how many PRs to ask for; a test seam.
    """
    try:
        listed = gh(
            "pr", "list", "--state", "open", "--limit", str(limit),
            "--json", "headRefName", "--jq", ".[].headRefName",
            check=False, cwd=repo,
        )  # fmt: skip
    except OSError, subprocess.TimeoutExpired:
        return None
    heads = listed.stdout.split()
    if listed.returncode != 0 or len(heads) >= limit:
        return None
    return set(heads)


def claimed_branches(repo: str) -> set[str] | None:
    """The branch every open claim in the findings register names, or None when unread.

    A checkout with no `scripts/dev/findings.py` has no register, so it reads as no claims.
    """
    findings = Path(repo) / "scripts" / "dev" / "findings.py"
    if not findings.is_file():
        return set()
    try:
        proc = subprocess.run(
            [sys.executable, str(findings), "claims", "--json"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        rows = json.loads(proc.stdout) if proc.returncode == 0 else None
    except OSError, subprocess.TimeoutExpired, ValueError:
        return None
    if not isinstance(rows, list):
        return None
    return {row["worktree"] for row in rows}


REGISTERS: tuple[tuple[str, Callable[[str], set[str] | None]], ...] = (
    ("the open PRs", open_pr_heads),
    ("the claims", claimed_branches),
)


def main(argv: list[str] | None = None, registers=REGISTERS) -> int:
    """Print the held branches; exit 1 when a register could not be read.

    Args:
        argv: the command line, without the program name.
        registers: each register's name and its reader; a test seam.
    """
    ArgumentParser(description=__doc__).parse_args(argv)
    repo = primary_checkout() or str(REPO)
    held: set[str] = set()
    unread = []
    for register, read in registers:
        branches = read(repo)
        if branches is None:
            unread.append(register)
        else:
            held |= branches
    if unread:
        print(f"{' and '.join(unread)} could not be read", file=sys.stderr)
        return FAILED
    for branch in sorted(held):
        print(branch)
    return OK


if __name__ == "__main__":
    sys.exit(main())
