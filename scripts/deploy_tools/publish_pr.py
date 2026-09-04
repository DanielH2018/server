#!/usr/bin/env python3
"""Publish a cron's local commit as an auto-merging pull request.

The three unattended crons that commit -- docs-refresh, eval-run and secret-rotate -- each
carried this sequence inline, byte for byte, in a template that also carries a Kuma push
token. A repository ruleset rejects every direct write to master, so the only way a cron
lands anything is: put the commit on a fresh branch, push that, take the commit back off
the local master so gitops-deploy's ``--ff-only`` still succeeds when the squash lands
under a new SHA, open the PR, and enable auto-merge.

``publish`` expects the commit to already be at HEAD of the primary checkout; the caller
made it, because what to stage and how to word it is the cron's business. It prints ONE
line to stdout that the caller can alert with, and its exit code says what state the
tree is in:

  0  branch pushed, PR opened, auto-merge enabled; the local commit is gone
  1  the branch never reached origin -- the commit is still local on HEAD. Nothing to
     clean up on origin; the next run refuses on the dirty/ahead tree
  2  the branch IS on origin but the PR could not be opened or auto-merge could not be
     enabled -- and the local commit is already gone. This is the state the secret-rotate
     audit's ``git ls-remote`` arm exists to see (a branch with no PR)

``open-pr`` prints the number of the first open PR whose head starts with the prefix, or
nothing. A ``gh`` failure prints nothing, exactly as the inline ``|| true`` did: the guard
is "do not stack on an open PR", and an unreachable GitHub is reported by the publish step
that follows, not here.

Run: uv run pytest scripts/deploy_tools/tests/test_publish_pr.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import gh as gh_mod
from lib import git as git_mod

# Same bound the shell's `tr '\n' ' ' | tail -c 400` applied: enough to carry the ruleset's
# rejection line, short enough for a Kuma message.
FAILURE_TAIL = 400

RC_PUBLISHED = 0
RC_STILL_LOCAL = 1
RC_PUSHED_NO_PR = 2

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Tools:
    """The two process boundaries, injectable so a test drives the sequence without a remote."""

    git: Runner
    gh: Runner


def real_tools(repo: Path) -> Tools:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return git_mod.git(*args, cwd=repo, check=False)

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return gh_mod.gh(*args, check=False)

    return Tools(git=git, gh=gh)


@dataclass(frozen=True)
class Outcome:
    rc: int
    message: str
    branch: str


def failure_tail(proc: subprocess.CompletedProcess[str]) -> str:
    """The last ``FAILURE_TAIL`` characters of a failed command's output, on one line."""
    text = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
    return text[-FAILURE_TAIL:]


def branch_name(prefix: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d-%H%M")
    return f"{prefix}{stamp}"


def publish(
    prefix: str,
    title: str,
    body: str,
    tools: Tools,
    now: datetime | None = None,
) -> Outcome:
    """Move HEAD's commit onto ``<prefix><stamp>``, push it, and open an auto-merging PR."""
    branch = branch_name(prefix, now)

    proc = tools.git("branch", branch, "HEAD")
    if proc.returncode != 0:
        return Outcome(
            RC_STILL_LOCAL,
            f"could not create {branch}; commit is local on master: {failure_tail(proc)}",
            branch,
        )

    proc = tools.git("push", "-u", "origin", branch)
    if proc.returncode != 0:
        return Outcome(
            RC_STILL_LOCAL,
            f"publishing {branch} failed; commit is local on master: {failure_tail(proc)}",
            branch,
        )

    # The branch is on origin. Take the commit off the local master BEFORE opening the PR:
    # once the squash lands under a new SHA, a local master that is one commit ahead makes
    # gitops-deploy's --ff-only refuse, and that parked the deployer twice during diagnosis.
    # HEAD~1, not origin/master: this undoes exactly the one commit the caller made.
    # origin/master is only as fresh as the last fetch, which the callers do not do, so
    # resetting to it could discard something else or move master backwards. The local
    # branch ref goes too, with -D because master no longer contains its commit; keeping it
    # would leave one dead branch per run.
    tools.git("reset", "--hard", "HEAD~1")
    tools.git("branch", "-D", branch)

    proc = tools.gh("pr", "create", "--head", branch, "--title", title, "--body", body)
    if proc.returncode != 0:
        return Outcome(
            RC_PUSHED_NO_PR,
            f"{branch} published but PR creation failed: {failure_tail(proc)}",
            branch,
        )

    proc = tools.gh("pr", "merge", "--auto", "--squash", "--delete-branch", branch)
    if proc.returncode != 0:
        return Outcome(
            RC_PUSHED_NO_PR,
            f"PR opened for {branch} but auto-merge could not be enabled: {failure_tail(proc)}",
            branch,
        )

    return Outcome(RC_PUBLISHED, f"PR opened for {branch} with auto-merge", branch)


def open_pr(prefix: str, tools: Tools) -> str:
    """The number of the first open PR whose head branch starts with ``prefix``, else ``""``."""
    proc = tools.gh("pr", "list", "--state", "open", "--json", "number,headRefName")
    if proc.returncode != 0:
        return ""
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    for pr in prs:
        if str(pr.get("headRefName", "")).startswith(prefix):
            return str(pr["number"])
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="the checkout holding the commit at HEAD (default: cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser(
        "publish", help="push HEAD's commit as a branch and open the PR"
    )
    pub.add_argument(
        "--prefix", required=True, help='branch prefix, e.g. "docs-refresh/"'
    )
    pub.add_argument("--title", required=True)
    body = pub.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)

    opn = sub.add_parser(
        "open-pr", help="print the number of an open PR under the prefix"
    )
    opn.add_argument("--prefix", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = real_tools(args.repo)
    if args.command == "open-pr":
        print(open_pr(args.prefix, tools), end="")
        return 0
    body = args.body if args.body is not None else args.body_file.read_text()
    outcome = publish(args.prefix, args.title, body, tools)
    print(outcome.message)
    return outcome.rc


if __name__ == "__main__":
    sys.exit(main())
