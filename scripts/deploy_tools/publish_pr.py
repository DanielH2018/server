#!/usr/bin/env python3
"""Publish a cron's local commit as a pull request, and start the landing that merges it.

The three unattended crons that commit -- docs-refresh, eval-run and secret-rotate -- each
carried this sequence inline, byte for byte, in a template that also carries a Kuma push
token. A repository ruleset rejects every direct write to master, so the only way a cron
lands anything is: put the commit on a fresh branch, push that, take the commit back off
the local master so gitops-deploy's ``--ff-only`` still succeeds when the squash lands
under a new SHA, open the PR, and hand it to a detached ``land.py`` that merges it.

WHY A LANDING AND NOT ``gh pr merge --auto``. The master review gate ruleset requires an
approving review, and GitHub's auto-merge never applies a ruleset bypass, so an armed cron PR
waits for an approval nobody is asked to give (#3609). ``gh pr merge`` also refuses such a PR at
its own pre-flight. ``land.py --arm-merge --await-merge`` is the path that merges past the
review gate: it waits for CI and merges through the REST endpoint, pinned to the head SHA it
read green. ``--detach`` matters as much as the merge. The callers hold the git-tree lock on
fd 9 and read this script's output through a pipe, so a landing that inherited either would
keep the lock for the whole CI wait. The detached landing closes every inherited descriptor
from 3 up and rebinds its stdio to its own log (``land_lib/detach.py``).

``publish`` expects the commit to already be at HEAD of the primary checkout; the caller
made it, because what to stage and how to word it is the cron's business. It prints ONE
line to stdout that the caller can alert with, and its exit code says what state the
tree is in:

  0  branch pushed, PR opened, landing started; the local commit is gone
  1  the branch never reached origin -- the commit is still local on HEAD. Nothing to
     clean up on origin; the next run refuses on the dirty/ahead tree
  2  the branch IS on origin but the PR could not be opened or its landing could not be
     started -- and the local commit is already gone. This is the state the secret-rotate
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
which is what the three crons call; the subcommand stays because the lookup on its own is
the useful thing to run by hand.

``unlanded`` answers "is there work from a previous run that never landed", which is the
guard the three crons need BEFORE they regenerate and commit. It reads origin rather than
the open-PR list because ``gh pr create`` runs after the push: a create failure leaves
``<prefix><stamp>`` on origin with no PR and no local trace at all, and an open-PR check
passes cleanly in exactly that state. Merged branches are deleted on this repo
(``deleteBranchOnMerge``), so a surviving head always means unlanded work. Its exit codes:

  0  nothing unlanded; it prints nothing
  1  origin could not be read -- fail closed, because ``|| true`` on an unreachable origin
     reads as "no stale branch" and publishes straight into the state this refuses
  2  a branch is on origin and its PR is open -- benign, its landing is waiting on CI. A PR
     whose checks are already green and which waits only on the review gate is merged here,
     the backstop for a landing that died or never started (see ``merge_if_stalled``)
  3  a branch is on origin with NO open PR -- the state a create failure leaves behind, and
     the one a human has to clear

Run: uv run pytest scripts/deploy_tools/tests/test_publish_pr.py
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.exit_codes import (
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
# says what it found on origin. Both are defined in `lib/exit_codes.py`, whose prefixes are
# what tell a reader which of the two a value belongs to.

# What a PR lookup reports when it timed out. Non-empty on purpose: `unlanded` and the
# `open-pr` shell idiom both read an empty answer as "no PR", which would let a run publish a
# second branch on the strength of an answer GitHub never gave.
OPEN_PR_UNKNOWN = "unknown"

# `lib.git.git` has no default timeout, and the callers hold the git-tree lock
# while this runs -- the GitOps deployer waits only `flock -w 180` for that lock. git sets no
# connect timeout of its own, so a blackholed origin would park the deployer rather than skip a
# run. 30s is well under both that wait and the 60s bound `lib.gh.gh` puts on the PR lookup
# beside it.
LS_REMOTE_TIMEOUT_S = 30.0

# The landing entry point. Run with this interpreter, not through `land.sh`: that wrapper
# execs `uv` from PATH, and docs-refresh's cron PATH does not carry ~/.local/bin.
LAND_SCRIPT = Path(__file__).resolve().parent / "land.py"

# `land.py --detach` resolves `--since`, forks and returns; it does not wait for anything.
LAND_START_TIMEOUT_S = 60.0

# `gh pr create` prints the new PR's URL as its last line.
PR_URL = re.compile(r"/pull/(\d+)\s*$")

# A check that finished without blocking a merge. A StatusContext reports `state` instead.
PASSING = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

# The shell's convention for "the command was killed on a timeout". `lib.gh.gh` bounds every
# call at 60s where the inline shell these crons carried had no bound at all, so this is a
# state the callers did not have before and must not meet as a traceback.
GH_TIMEOUT_RC = 124

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PublishTools:
    """The process boundaries, injectable so a test drives the sequence without a remote."""

    git: Runner
    gh: Runner
    land: Runner


def real_tools(repo: Path, land_script: Path = LAND_SCRIPT) -> PublishTools:
    def git(
        *args: str, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return git_mod.git(*args, cwd=repo, check=False, timeout=timeout)

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return gh_mod.gh(*args, check=False)

    def land(*args: str) -> subprocess.CompletedProcess[str]:
        # The landing deploys through deploy.sh and ansible, which need `uv` and kubectl on
        # PATH; cron provides neither directory.
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [
                str(Path.home() / ".local/bin"),
                "/usr/local/bin",
                env.get("PATH", "/usr/bin:/bin"),
            ]
        )
        return subprocess.run(
            [sys.executable, str(land_script), *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=LAND_START_TIMEOUT_S,
        )

    return PublishTools(git=git, gh=gh, land=land)


@dataclass(frozen=True)
class PublishOutcome:
    rc: int
    message: str
    branch: str


def failure_tail(proc: subprocess.CompletedProcess[str]) -> str:
    """The last ``FAILURE_TAIL`` characters of a failed command's output, on one line."""
    text = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
    return text[-FAILURE_TAIL:]


def run_bounded(
    tool: Runner, name: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """``tool(*args)`` with a timeout reported as a failed process rather than a raise.

    Both ``gh`` calls in ``publish`` sit AFTER the push and after ``reset --hard HEAD~1``, so
    the true state at a timeout is branch-on-origin / commit-gone / no-PR -- exit 2. A raise
    propagates out of ``main`` as exit 1 with a traceback, which is the code that promises the
    commit is still local and there is nothing to clean up on origin.
    """
    try:
        return tool(*args)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=[name, *args],
            returncode=GH_TIMEOUT_RC,
            stdout="",
            stderr=f"{name} {' '.join(args[:2])} timed out after {exc.timeout}s",
        )


def run_gh(tools: PublishTools, *args: str) -> subprocess.CompletedProcess[str]:
    return run_bounded(tools.gh, "gh", *args)


def pr_number(create_output: str) -> str:
    """The PR number from ``gh pr create``'s output, or ``""`` when it printed no PR URL."""
    lines = (create_output or "").strip().splitlines()
    match = PR_URL.search(lines[-1]) if lines else None
    return match.group(1) if match else ""


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
    """Move HEAD's commit onto ``<prefix><stamp>``, push it, open a PR and start its landing."""
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
        return PublishOutcome(
            PUBLISH_PUSHED_NO_PR,
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

    number = pr_number(proc.stdout)
    if not number:
        return PublishOutcome(
            PUBLISH_PUSHED_NO_PR,
            f"PR opened for {branch} but gh printed no PR number, so no landing was started: "
            f"{failure_tail(proc)}",
            branch,
        )

    # A REST merge does not delete the branch the way `gh pr merge --delete-branch` did; the
    # repo's deleteBranchOnMerge does, and `unlanded` relies on that.
    proc = run_bounded(
        tools.land,
        "land.py",
        "--pr",
        number,
        "--arm-merge",
        "--await-merge",
        "--detach",
    )
    if proc.returncode != 0:
        return PublishOutcome(
            PUBLISH_PUSHED_NO_PR,
            f"PR #{number} opened for {branch} but its landing could not be started: "
            f"{failure_tail(proc)}",
            branch,
        )

    return PublishOutcome(
        PUBLISH_PUBLISHED, f"PR opened for {branch}; landing PR #{number}", branch
    )


def merge_if_stalled(number: str, tools: PublishTools) -> str:
    """Merge an open PR that waits only on the review gate, at the head its green checks ran on.

    The backstop for a landing that died or never started. ``publish`` starts one per PR, so in
    the normal case this finds checks still running and does nothing. A PR whose checks are
    green but whose review is still required would otherwise stay open, and every later run of
    its cron would skip on it. The merge is the same REST call ``land_lib/merge.py`` makes,
    pinned with ``sha`` so GitHub refuses it if the head moved after the checks were read.

    Returns:
      ``""`` when the PR is not stalled: checks pending or failing, no review required, or a
      lookup that failed. Otherwise a clause for the caller's message saying the PR was merged
      or why the merge was refused.
    """
    proc = run_gh(
        tools,
        "pr",
        "view",
        number,
        "--json",
        "headRefOid,reviewDecision,statusCheckRollup",
    )
    if proc.returncode != 0:
        return ""
    try:
        view = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    checks = view.get("statusCheckRollup") or []
    # An empty rollup is pending, never green: CI has not registered its checks yet.
    green = bool(checks) and all(
        (c.get("conclusion") or c.get("state") or "") in PASSING for c in checks
    )
    head = str(view.get("headRefOid") or "")
    if view.get("reviewDecision") != "REVIEW_REQUIRED" or not green or not head:
        return ""
    proc = run_gh(
        tools,
        "api",
        "-X",
        "PUT",
        f"repos/{{owner}}/{{repo}}/pulls/{number}/merge",
        "-f",
        "merge_method=squash",
        "-f",
        f"sha={head}",
    )
    if proc.returncode != 0:
        return f"its checks are green but the merge was refused: {failure_tail(proc)}"
    return f"its checks were green and it waited only on review, so it was merged at {head[:8]}"


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
        # Still UNLANDED_PR_OPEN after a merge: this run's tree predates the merge, so it skips
        # and the next run starts from a master that holds the merged commit.
        merged = merge_if_stalled(number, tools)
        return PublishOutcome(
            UNLANDED_PR_OPEN,
            f"PR #{number} from a previous run is still open ({branch})"
            + (f"; {merged}" if merged else ""),
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
    pub.add_argument(
        "--land-script",
        type=Path,
        default=LAND_SCRIPT,
        help="the landing entry point, run with this interpreter (default: the sibling land.py)",
    )
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
    tools = real_tools(args.repo, getattr(args, "land_script", LAND_SCRIPT))
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
