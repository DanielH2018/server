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
     audit's ``git ls-remote`` arm exists to see (a branch with no PR). Also covers the
     rarer case where ``reset --hard HEAD~1`` itself failed after the push: the branch is
     on origin, but the local commit is NOT gone -- master is still one commit ahead of
     origin, named as such in the message, and the run stops before attempting a PR

``open-pr`` prints the number of the first open PR whose head starts with the prefix, or
nothing. A ``gh`` failure prints nothing, exactly as the inline ``|| true`` did: the guard
is "do not stack on an open PR", and an unreachable GitHub is reported by the publish step
that follows, not here. A ``gh`` TIMEOUT is the one exception and prints ``unknown``, which
is non-empty so a caller gating on ``[ -n "$OPEN_PR" ]`` refuses rather than publishing a
second branch -- see ``OPEN_PR_UNKNOWN``. That sentinel is load-bearing inside ``unlanded``,
which is what the three crons call now; the subcommand stays because the lookup on its own
is the useful thing to run by hand.

``unlanded`` answers "is there work from a previous run that never landed", which is the
guard the three crons need BEFORE they regenerate and commit. It reads origin rather than
the open-PR list because ``gh pr create`` runs after the push: a create failure leaves
``<prefix><stamp>`` on origin with no PR and no local trace at all, and an open-PR check
passes cleanly in exactly that state. Merged branches are deleted on this repo
(``deleteBranchOnMerge``), so a surviving head always means unlanded work. Its exit codes:

  0  nothing unlanded; it prints nothing
  1  origin could not be read -- fail closed, because ``|| true`` on an unreachable origin
     reads as "no stale branch" and publishes straight into the state this refuses
  2  a branch is on origin and its PR is open -- benign, it is waiting on CI
  3  a branch is on origin with NO open PR -- the state a create failure leaves behind, and
     the one a human has to clear

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

from deploy_tools.exit_codes import (
    PUBLISH_PUBLISHED,
    PUBLISH_PUSHED_NO_PR,
    PUBLISH_STILL_LOCAL,
    UNLANDED_NO_PR,
    UNLANDED_NOTHING,
    UNLANDED_ORIGIN_UNREADABLE,
    UNLANDED_PR_OPEN,
)
from lib import gh as gh_mod
from lib import git as git_mod

# Same bound the shell's `tr '\n' ' ' | tail -c 400` applied: enough to carry the ruleset's
# rejection line, short enough for a Kuma message.
FAILURE_TAIL = 400

# Two contracts over the same integers -- `publish` says what state the tree is in, `unlanded`
# says what it found on origin. Both are defined in `deploy_tools/exit_codes.py`, whose
# prefixes are what tell a reader which of the two a value belongs to; the old names here were
# `RC_*` for both, so 2 read as one number with two meanings.

# What a PR lookup reports when it timed out. Non-empty on purpose: `unlanded` and the
# `open-pr` shell idiom both read an empty answer as "no PR", which would let a run publish a
# second branch on the strength of an answer GitHub never gave.
OPEN_PR_UNKNOWN = "unknown"

# `lib.git.git` has no default timeout, and the callers hold /var/lock/server-git-tree.lock
# while this runs -- the GitOps deployer waits only `flock -w 180` for that lock. git sets no
# connect timeout of its own, so a blackholed origin would park the deployer rather than skip a
# run. 30s is well under both that wait and the 60s bound `lib.gh.gh` puts on the PR lookup
# beside it.
LS_REMOTE_TIMEOUT_S = 30.0

# The shell's convention for "the command was killed on a timeout". `lib.gh.gh` bounds every
# call at 60s where the inline shell these crons carried had no bound at all, so this is a
# state the callers did not have before and must not meet as a traceback.
GH_TIMEOUT_RC = 124

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PublishTools:
    """The two process boundaries, injectable so a test drives the sequence without a remote."""

    git: Runner
    gh: Runner


def real_tools(repo: Path) -> PublishTools:
    def git(
        *args: str, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return git_mod.git(*args, cwd=repo, check=False, timeout=timeout)

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return gh_mod.gh(*args, check=False)

    return PublishTools(git=git, gh=gh)


@dataclass(frozen=True)
class PublishOutcome:
    rc: int
    message: str
    branch: str


def failure_tail(proc: subprocess.CompletedProcess[str]) -> str:
    """The last ``FAILURE_TAIL`` characters of a failed command's output, on one line."""
    text = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
    return text[-FAILURE_TAIL:]


def run_gh(tools: PublishTools, *args: str) -> subprocess.CompletedProcess[str]:
    """``tools.gh(*args)`` with a timeout reported as a failed process rather than a raise.

    Both ``gh`` calls in ``publish`` sit AFTER the push and after ``reset --hard HEAD~1``, so
    the true state at a timeout is branch-on-origin / commit-gone / no-PR -- exit 2. A raise
    propagates out of ``main`` as exit 1 with a traceback, which is the code that promises the
    commit is still local and there is nothing to clean up on origin.
    """
    try:
        return tools.gh(*args)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=GH_TIMEOUT_RC,
            stdout="",
            stderr=f"gh {' '.join(args[:2])} timed out after {exc.timeout}s",
        )


def branch_name(prefix: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d-%H%M")
    return f"{prefix}{stamp}"


def publish(
    prefix: str,
    title: str,
    body: str,
    tools: PublishTools,
    now: datetime | None = None,
) -> PublishOutcome:
    """Move HEAD's commit onto ``<prefix><stamp>``, push it, and open an auto-merging PR."""
    branch = branch_name(prefix, now)

    proc = tools.git("branch", branch, "HEAD")
    if proc.returncode != 0:
        return PublishOutcome(
            PUBLISH_STILL_LOCAL,
            f"could not create {branch}; commit is local on master: {failure_tail(proc)}",
            branch,
        )

    proc = tools.git("push", "-u", "origin", branch)
    if proc.returncode != 0:
        # The branch never reached origin, so it is a dead local ref pointing at the same
        # commit as master. Left behind, a retry inside the same UTC minute fails at
        # `git branch` with "already exists" and reports the wrong cause. Best-effort: the
        # push already failed, so there is nothing more informative to do if this fails too.
        tools.git("branch", "-D", branch)
        return PublishOutcome(
            PUBLISH_STILL_LOCAL,
            f"publishing {branch} failed; commit is local on master: {failure_tail(proc)}",
            branch,
        )

    # The branch is on origin. Take the commit off the local master BEFORE opening the PR:
    # once the squash lands under a new SHA, a local master that is one commit ahead makes
    # gitops-deploy's --ff-only refuse, and that parked the deployer twice during diagnosis.
    # HEAD~1, not origin/master: this undoes exactly the one commit the caller made.
    # origin/master is only as fresh as the last fetch, which the callers do not do, so
    # resetting to it could discard something else or move master backwards.
    proc = tools.git("reset", "--hard", "HEAD~1")
    if proc.returncode != 0:
        # The branch is on origin, but master is still one commit ahead -- exactly the state
        # this reset exists to prevent. Report it as rc 2 (branch published, human must
        # clear it) rather than pressing on to open a PR while master disagrees with origin;
        # `failure_tail` names why the reset itself failed (index lock, dirtied tree).
        return Outcome(
            RC_PUSHED_NO_PR,
            f"{branch} pushed but resetting local master to drop its commit failed; "
            f"master is still one commit ahead of origin until this is cleared by hand: "
            f"{failure_tail(proc)}",
            branch,
        )
    tools.git("branch", "-D", branch)

    proc = run_gh(
        tools, "pr", "create", "--head", branch, "--title", title, "--body", body
    )
    if proc.returncode != 0:
        return PublishOutcome(
            PUBLISH_PUSHED_NO_PR,
            f"{branch} published but PR creation failed: {failure_tail(proc)}",
            branch,
        )

    proc = run_gh(tools, "pr", "merge", "--auto", "--squash", "--delete-branch", branch)
    if proc.returncode != 0:
        return PublishOutcome(
            PUBLISH_PUSHED_NO_PR,
            f"PR opened for {branch} but auto-merge could not be enabled: {failure_tail(proc)}",
            branch,
        )

    return PublishOutcome(
        PUBLISH_PUBLISHED, f"PR opened for {branch} with auto-merge", branch
    )


def open_pr(prefix: str, tools: PublishTools, branch: str = "") -> str:
    """The number of the first open PR whose head branch starts with ``prefix``, else ``""``.

    Args:
      prefix: the branch prefix to match a head against.
      tools: the process boundaries.
      branch: when given, require this EXACT head instead of the prefix. ``unlanded`` needs
        that: several heads can sit under one prefix, and a PR open on a different one would
        otherwise clear an orphan and name the wrong branch in the message.

    Returns:
      The PR number as a string, ``""`` when nothing matched, or ``OPEN_PR_UNKNOWN`` when the
      lookup timed out. The anonymous GitHub quota is 60/hour and shared per host, so a slow
      ``pr list`` is reachable in normal operation, and every caller reads an empty answer as
      "no PR is open, publish another branch".
    """
    try:
        proc = tools.gh("pr", "list", "--state", "open", "--json", "number,headRefName")
    except subprocess.TimeoutExpired:
        return OPEN_PR_UNKNOWN
    if proc.returncode != 0:
        return ""
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    for pr in prs:
        head = str(pr.get("headRefName", ""))
        matched = head == branch if branch else head.startswith(prefix)
        if matched:
            return str(pr["number"])
    return ""


def unlanded(prefix: str, tools: PublishTools) -> PublishOutcome:
    """Whether a previous run's branch is still on origin, and whether it has an open PR.

    ``git ls-remote`` decides; the PR number only labels the finding. That order is
    deliberate twice over. ``ls-remote`` authenticates over git's own credential path rather
    than through ``gh``, so the common (clean) case spends nothing from the shared 60/hour
    GitHub quota that makes ``gh`` time out in the first place. And the ABSENCE of a PR is
    the interesting case, so a PR lookup that fails must not be able to clear the branch.
    """
    try:
        proc = tools.git(
            "ls-remote",
            "--heads",
            "origin",
            f"{prefix}*",
            timeout=LS_REMOTE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return PublishOutcome(
            UNLANDED_ORIGIN_UNREADABLE,
            f"origin did not answer within {LS_REMOTE_TIMEOUT_S:.0f}s when checking for an "
            f"unlanded {prefix}* branch",
            "",
        )
    if proc.returncode != 0:
        return PublishOutcome(
            UNLANDED_ORIGIN_UNREADABLE,
            f"cannot reach origin to check for an unlanded {prefix}* branch: {failure_tail(proc)}",
            "",
        )
    heads = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not heads:
        return PublishOutcome(UNLANDED_NOTHING, "", "")

    branch = heads[0].split("refs/heads/", 1)[-1].strip()
    number = open_pr(prefix, tools, branch=branch)
    if number == OPEN_PR_UNKNOWN:
        return PublishOutcome(
            UNLANDED_NO_PR,
            f"branch {branch} is on origin and the open-PR lookup did not answer; treating "
            f"it as unpublished. Check it and open the PR by hand if it has none",
            branch,
        )
    if number:
        return PublishOutcome(
            UNLANDED_PR_OPEN,
            f"PR #{number} from a previous run is still open ({branch})",
            branch,
        )
    return PublishOutcome(
        UNLANDED_NO_PR,
        f"branch {branch} is on origin with NO open PR — a previous run published it but "
        f"never opened one, and the local tree cannot show this. Open the PR by hand",
        branch,
    )


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

    unl = sub.add_parser(
        "unlanded", help="report a previous run's branch still sitting on origin"
    )
    unl.add_argument("--prefix", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = real_tools(args.repo)
    if args.command == "open-pr":
        print(open_pr(args.prefix, tools), end="")
        return 0
    if args.command == "unlanded":
        outcome = unlanded(args.prefix, tools)
        if outcome.message:
            print(outcome.message)
        return outcome.rc
    body = args.body if args.body is not None else args.body_file.read_text()
    outcome = publish(args.prefix, args.title, body, tools)
    print(outcome.message)
    return outcome.rc


if __name__ == "__main__":
    sys.exit(main())
