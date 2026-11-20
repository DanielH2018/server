#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.
The removal itself then refuses a tree that a live process still uses as its cwd or its
`CLAUDE_PROJECT_DIR`, naming the pid, so the report can say `removable` for a tree that
`--prune` then keeps. See lib.worktrees.processes_using.

It also sweeps the BRANCHES those worktrees leave behind. See judge_branches.

"Merged" is checked three ways, cheapest first: ancestry, then patch-id, then content. PRs
land here rebased or squashed, never fast-forwarded, so the branch tip is not an ancestor of
origin/master — on ancestry alone this script reported "nothing to remove" while merged trees
piled up. `git cherry` compares by patch-id and settles the rebase case. A squash defeats
both, because collapsing several commits into one leaves no patch-id to match; `git merge-tree`
settles that by asking whether merging the branch would change master at all. See is_merged.

Each verdict is cached in the git common dir, keyed by the head and master SHAs, so the
`--prune` run after a report, and the `findings.py reap` it starts, reuse the report's answers
instead of asking git and the forge again. Dirty, lock and process checks are always live. See
lib.worktrees.VERDICT_CACHE.

The lock is the interesting one. Claude Code locks a session's worktree with a reason
naming the owning process — `claude session <name> (pid 1285937 start 2164388)` — and does
not release it when the session ends. Treating any lock as "in use" would therefore keep
every abandoned worktree forever. The `start` field is the process start time from
/proc/<pid>/stat, so it distinguishes a live owner from a dead one whose pid has since been
reused, and a lock whose owner is gone is ignored rather than obeyed.
A fan-out batch's lock names its systemd user unit (`fanout-<batch>`) instead,
and reads as dead once that unit is inactive and the lock is a day old. See
lib.fanout_lock.

Usage:
    uv run python scripts/dev/prune_worktrees.py            # report only (default)
    uv run python scripts/dev/prune_worktrees.py --prune    # also remove the removable ones
    uv run python scripts/dev/prune_worktrees.py --brief    # short report, for a banner
    uv run python scripts/dev/prune_worktrees.py --gc       # object-store repair only
    uv run python scripts/dev/prune_worktrees.py --check            # the branch sweep's verdicts
    uv run python scripts/dev/prune_worktrees.py --check <branch>   # one branch's verdict

`--check` alone prints what the branch sweep would delete and why, and counts the session
branches it keeps under each reason. See judge_branches.

`--check <branch>` answers "does this branch still hold unlanded work" for one branch, with
the same four-layer ladder the pruner deletes by. It prints `landed (<layer>)` or `unlanded`
and exits 0 or 1; an unknown branch, or no repository, exits 64. Both forms read only, and
remove nothing.

`--brief` chooses the report's shape and nothing else. It is orthogonal to `--prune`:
`--prune --brief` removes the same worktrees `--prune` alone would, and prints a short
report of what it removed.

A prune also repairs the shared object store the removed worktrees leave litter in, and
`--gc` runs that repair alone. See repair_object_store.

An orphan directory, one under .claude/worktrees/ that git no longer registers, is removed
too when it holds nothing git lacks. One that holds content git never stored is kept, and the
report names each such file. See dev.worktree_orphans.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


from dev.foreign_owned import foreign_owned_advice
from dev.worktree_orphans import (
    find_orphan_dirs,
    orphan_report,
    prune_orphans,
    unstored_files,
)
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK, USAGE_ERROR
from lib.git import git, git_stdout, repair_object_store
from lib.progress import count_line
from lib.worktree_label import with_label
from lib.worktree_owner import SESSION_BRANCH_PREFIXES

# The worktree library: the readers, `is_merged` and `classify`. This file is only the CLI
# over it, and nothing else imports this file -- every other consumer imports `lib.worktrees`.
from lib.worktrees import (
    IN_USE,
    REMOVABLE,
    Worktree,
    classify,
    is_dirty,
    is_merged,
    merged_layer,
    open_pr_heads,
    parse_worktree_list,
    primary_checkout,
    remove,
)

STALE = "stale"
VANISHED = "vanished"


def orphan_verdicts(repo: str) -> list[tuple[str, list[str] | None]]:
    """(path, unstored_files) for every orphan directory under the checkout's worktrees dir."""
    tracked = {
        t.path
        for t in parse_worktree_list(
            git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
        )
    }
    return [
        (path, unstored_files(repo, path))
        for path in find_orphan_dirs(str(Path(repo) / ".claude" / "worktrees"), tracked)
    ]


def session_branches(repo: str) -> dict[str, str]:
    """Every local session branch and its tip SHA, in ref order.

    `lib.worktree_owner.SESSION_BRANCH_PREFIXES` is the outer refusal: a branch outside it is
    not a session branch and the sweep never touches it.
    """
    lines = git_stdout(
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        *(f"refs/heads/{prefix}*" for prefix in SESSION_BRANCH_PREFIXES),
        cwd=repo,
        check=False,
    ).splitlines()
    return dict(line.split() for line in lines)


def held_branches(repo: str) -> set[str]:
    """The branches a worktree has checked out, the primary checkout's among them."""
    return {
        tree.branch
        for tree in parse_worktree_list(
            git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
        )
        if tree.branch
    }


def orphan_branches(repo: str) -> list[str]:
    """Local session branches that no worktree has checked out.

    Excluding a held branch is also what makes the current branch safe: the primary checkout
    is a worktree.
    """
    held = held_branches(repo)
    return [branch for branch in session_branches(repo) if branch not in held]


def ancestry_landed_branches(repo: str) -> set[str]:
    """Every local branch whose tip is an ancestor of origin/master, in ONE git call.

    The bulk layer, and the reason the sweep is cheap enough to run from the SessionStart
    banner.

    An empty set on failure, which reads as "nothing settled" — the KEEP direction, because
    every caller of this decides what to delete.
    """
    result = git(
        "branch",
        "--merged",
        "origin/master",
        "--format=%(refname:short)",
        cwd=repo,
        check=False,
    )
    return set(result.stdout.split()) if result.returncode == 0 else set()


def claimed_branches(repo: str) -> set[str] | None:
    """The branch every open claim in the findings register names, or None when unread.

    Stale claims count too. `findings.py claims` exits 0 when its worktree read failed, and
    every claim then reads stale, so its `live` field cannot clear a branch for deletion. A
    stale claim holds its branch only until `findings.py reap` releases it.

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


HELD = "a worktree holds it"
UNLANDED = "not landed, or the landed state could not be read"
OPEN_PR = "an open PR has it as its head"
OPEN_PRS_UNREAD = "the open PRs could not be read"
CLAIMED = "a claim in the findings register names it"
CLAIMS_UNREAD = "the claims could not be read"


@dataclass(frozen=True)
class BranchVerdict:
    """What the sweep does with one session branch.

    Attributes:
        branch: the local branch name.
        delete: whether `--prune` deletes it.
        reason: `landed by <layer>` for a deletion, else the condition that keeps it.
    """

    branch: str
    delete: bool
    reason: str


def judge_branches(
    repo: str,
    layer: Callable[[str, str, str], str | None] = merged_layer,
    open_prs: Callable[[str], set[str] | None] = open_pr_heads,
    claimed: Callable[[str], set[str] | None] = claimed_branches,
) -> list[BranchVerdict]:
    """One verdict per local session branch: the judge `--prune` deletes branches by.

    A branch is deleted only when all four hold, read in this order:

    - no worktree holds it, by `git worktree list`;
    - its work is on origin/master, by one bulk `git branch --merged` and then
      `lib.worktrees.merged_layer` on the rest. That ladder's last layer asks the forge which
      head it merged, which settles a squash-merged branch master has since drifted from;
    - no open PR has it as its head, by one `gh pr list`;
    - no claim in the findings register names it, by `findings.py claims --json`.

    An answer that could not be read keeps the branch. The two register reads run only when
    some branch has passed the first two conditions, so a run with nothing to delete pays for
    neither.

    `layer`, `open_prs` and `claimed` are test seams.
    """
    held = held_branches(repo)
    ancestry = ancestry_landed_branches(repo)
    landed, kept = {}, {}
    for branch, head in session_branches(repo).items():
        if branch in held:
            kept[branch] = HELD
            continue
        how = "ancestry" if branch in ancestry else layer(repo, head, branch)
        if how is None:
            kept[branch] = UNLANDED
        else:
            landed[branch] = how
    with_pr = open_prs(repo) if landed else set()
    with_claim = claimed(repo) if landed else set()
    verdicts = [BranchVerdict(branch, False, reason) for branch, reason in kept.items()]
    for branch, how in landed.items():
        if with_pr is None:
            verdicts.append(BranchVerdict(branch, False, OPEN_PRS_UNREAD))
        elif branch in with_pr:
            verdicts.append(BranchVerdict(branch, False, OPEN_PR))
        elif with_claim is None:
            verdicts.append(BranchVerdict(branch, False, CLAIMS_UNREAD))
        elif branch in with_claim:
            verdicts.append(BranchVerdict(branch, False, CLAIMED))
        else:
            verdicts.append(BranchVerdict(branch, True, f"landed by {how}"))
    return verdicts


def landed_orphan_branches(repo: str, deep: bool) -> list[str]:
    """The orphan session branches the sweep may delete.

    `deep` chooses how hard to look, and the two callers want different answers. The banner
    passes False and gets the bulk ancestry layer alone — two git calls, no per-branch work
    and no network. It UNDERCOUNTS a branch that landed by rebase or squash, and counts one
    an open PR or a claim would keep. That is the right trade for a line whose job is to say
    "there is something to sweep": `--brief` is budgeted at 5s by
    `.claude/hooks/session-health.py` and measured at 1.3s warm.

    `--prune` and the long report pass True and get judge_branches' deletions, because a
    deletion has to be right per branch rather than in aggregate. A landed branch kept
    because a register could not be read is counted on stderr, so a host whose `gh` stopped
    answering does not keep every branch in silence.
    """
    if deep:
        verdicts = judge_branches(repo)
        for reason in (OPEN_PRS_UNREAD, CLAIMS_UNREAD):
            unread = sum(verdict.reason == reason for verdict in verdicts)
            if unread:
                print(f"kept {unread} landed branch(es): {reason}", file=sys.stderr)
        return [verdict.branch for verdict in verdicts if verdict.delete]
    ancestry = ancestry_landed_branches(repo)
    return [branch for branch in orphan_branches(repo) if branch in ancestry]


def check_sweep(show_all: bool) -> int:
    """Print what the branch sweep would delete and why, and count what it keeps.

    One count line for all session branches, one per branch form, then the branches a sweep
    would delete, grouped by the layer that settled them. A kept branch is counted under its
    reason, and `show_all` lists it.
    """
    repo = primary_checkout()
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return USAGE_ERROR
    # Deletions first, so each count line leads with them.
    verdicts = sorted(judge_branches(repo), key=lambda v: not v.delete)

    def state(verdict: BranchVerdict) -> str:
        verb = "would be deleted" if verdict.delete else "kept"
        return f"{verb} ({verdict.reason})"

    hidden = any(not v.delete for v in verdicts) and not show_all
    hint = "; --all lists the kept" if hidden else ""
    print(
        f"{len(verdicts)} session branch(es): "
        f"{count_line(state(v) for v in verdicts)}{hint}"
    )
    for prefix in SESSION_BRANCH_PREFIXES:
        mine = [v for v in verdicts if v.branch.startswith(prefix)]
        print(f"{prefix}*: {count_line(state(v) for v in mine)}")
    groups: dict[str, list[str]] = {}
    for verdict in verdicts:
        if verdict.delete or show_all:
            groups.setdefault(state(verdict), []).append(verdict.branch)
    for heading, names in groups.items():
        print(f"{heading}:")
        for name in names:
            print(f"  {name}")
    return OK


def delete_branch(repo: str, branch: str) -> tuple[bool, str]:
    """Delete one orphan branch, `-d` first and `-D` only if that refuses.

    `git branch -d` accepts ancestry only, so it refuses every branch that landed by rebase or
    squash — which is most of them here, and is why the dotfiles hook swept so few. `-D` throws
    away git's own backstop, so judge_branches IS the safety: this is never called for a
    branch it has not already settled.
    """
    result = git("branch", "-d", branch, cwd=repo, check=False)
    if result.returncode == 0:
        return True, ""
    forced = git("branch", "-D", branch, cwd=repo, check=False)
    return forced.returncode == 0, forced.stderr.strip()


def sweep_branches(repo: str, branches: list[str]) -> int:
    """Delete each already-settled orphan branch, printing one line per outcome.

    Takes the list rather than deriving it, so the caller's report and its removals cannot
    name different branches.

    Returns:
      How many branches were refused deletion, each named on stderr.
    """
    failed = 0
    for branch in branches:
        ok, error = delete_branch(repo, branch)
        if ok:
            print(f"deleted {branch}")
        else:
            print(f"could not delete {branch}: {error}", file=sys.stderr)
            failed += 1
    return failed


def survey(repo: str) -> list[tuple[str, Worktree, str]]:
    """(verdict, worktree, reason) for every session worktree, primary excluded.

    A registration whose directory is gone reads VANISHED and is never asked is_dirty, which
    runs git with the tree as its cwd and raised FileNotFoundError, taking the whole report and
    the weekly `--prune --brief` down with it. It is never REMOVABLE either: there is
    nothing to remove, and the `git worktree prune` in repair_object_store, at prune_all's
    tail, drops the registration on the next `--prune`.
    """
    trees = parse_worktree_list(
        git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
    )
    out = []
    for tree in trees[1:]:
        if not Path(tree.path).is_dir():
            out.append(
                (VANISHED, tree, "directory gone — `--prune` drops the registration")
            )
            continue
        verdict, reason = classify(
            tree,
            merged=is_merged(repo, tree.head, tree.branch or ""),
            dirty=is_dirty(tree.path),
        )
        out.append((verdict, tree, reason))
    return out


def survey_report(
    surveyed: list[tuple[str, Worktree, str]], show_all: bool
) -> tuple[list[str], list[Worktree], int]:
    """The long report's worktree lines, the removable trees and the vanished count.

    A kept worktree is counted, not listed, because it asks nothing of the reader.
    `show_all` lists it as well.
    """
    lines, removable, vanished, kept = [], [], 0, 0
    for verdict, tree, reason in surveyed:
        if verdict in (REMOVABLE, VANISHED) or show_all:
            lines.append(
                f"[{verdict:9}] {with_label(tree.path, tree.path)}\n            {reason}"
            )
        else:
            kept += 1
        if verdict == REMOVABLE:
            removable.append(tree)
        vanished += verdict == VANISHED
    if kept:
        lines.append(f"{kept} worktree(s) kept; --all lists them")
    return lines, removable, vanished


def reap_claims(repo: str) -> list[str]:
    """Run the checkout's own `findings.py reap` and return the lines to print.

    A removed worktree's claims read as stale from then on, but only `reap` releases them, and
    nothing ran it outside a fan-out's launch. It is the checkout's copy, so a scratch
    repo with no `scripts/dev/findings.py` reaps nothing and says so.
    """
    findings = Path(repo) / "scripts" / "dev" / "findings.py"
    if not findings.is_file():
        return [f"claims not reaped: no {findings}"]
    # This interpreter, not `uv`: the weekly timer's PATH omits ~/.local/bin, and it already
    # runs this script under `uv run`, so sys.executable is the repo's env.
    try:
        proc = subprocess.run(
            [sys.executable, str(findings), "reap"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ["claims not reaped: reap ran past 300s"]
    lines = proc.stdout.splitlines()
    if proc.returncode != 0:
        detail = " ".join(proc.stderr.split())
        lines.append(f"claims not reaped: reap exited {proc.returncode}: {detail}")
    return lines


def skip_reap(repo: str) -> list[str]:
    """The reaper `--no-reap` hands `prune_all`: it releases nothing and says why."""
    return ["claims not reaped: --no-reap"]


def prune_all(
    repo: str,
    trees: list[Worktree],
    advise: Callable[[str], list[str]] = foreign_owned_advice,
    remover: Callable[[str, Worktree], tuple[bool, str]] | None = None,
    reaper: Callable[[str], list[str]] = reap_claims,
) -> int:
    """Remove each tree, printing one line per outcome.

    Shared by both report shapes so that `--prune` cannot mean one thing with `--brief` and
    another without it. A failed removal goes to stderr, followed by `advise`'s lines, which
    name any path the removal could not delete because another uid owns it.

    `remover` is `lib.worktrees.remove` unless a test hands it another, read at call time so
    a test of `main` that replaces this module's `remove` still reaches it.

    Returns:
      How many of `trees` failed to be removed. A tree a live process still uses is named on
      stderr but not counted: one session running at the weekly run's hour would otherwise
      keep its Healthchecks.io check down for a week, and the next run removes the tree once
      its holder exits.
    """
    remover = remove if remover is None else remover
    removed = failed = 0
    for tree in trees:
        ok, error = remover(repo, tree)
        if ok:
            removed += 1
            print(f"removed {tree.path}")
        else:
            if not error.startswith(IN_USE):
                failed += 1
            print(f"could not remove {tree.path}: {error}", file=sys.stderr)
            for line in advise(tree.path):
                print(line, file=sys.stderr)
    for line in repair_object_store(repo):
        print(line)
    if removed:
        for line in reaper(repo):
            print(line)
    return failed


def brief(prune: bool = False, reap: bool = True) -> int:
    """One line per removable worktree; silent when there is nothing to remove.

    Returns 0, or with `prune` 1 when any removal or branch deletion failed. A worktree a
    live process holds is a skip, not a failure. `reap` False removes without running
    `findings.py reap` afterwards.

    This exists for the SessionStart banner, which prints nothing on a healthy day and has to
    stay cheap to read. Claude Code's own worktree keeper already lists every branch whose
    commits landed by squash or rebase, but each line ends by asking the reader to go run
    `gh pr list --state merged --head <branch>` themselves — the lookup is_merged() already
    performs. This prints the answer instead of the homework.

    # DECIDED: the banner REPORTS, the weekly timer PRUNES. Both halves of the sweep are
    # automatic, and neither runs from a SessionStart hook. `--prune` removes worktrees and
    # deletes branches in the PRIMARY checkout, which every concurrent session shares, and it
    # ends in `repair_object_store` — a `git gc` whose duration is unbounded against a hook
    # budgeted at 5s in `.claude/hooks/session-health.py`. Running it at every session start
    # would put N sessions' removals in a race for a saving one weekly run already gets. The
    # weekly timer of the `worktree-sweep` host component is the arm that removes, for
    # this reason: a worktree-isolated session's git commands are refused against the
    # primary checkout, so the sessions that SEE the mess are the ones structurally unable to
    # clear it. Full reasoning in this docstring and in that component.
    """
    repo = primary_checkout()
    if repo is None:
        return 0
    removable = [
        (tree, reason) for verdict, tree, reason in survey(repo) if verdict == REMOVABLE
    ]
    # Shallow, and only for the report: the banner pays two git calls, not one per branch. See
    # landed_orphan_branches for what that undercounts and why it is the right trade here. The
    # pruning path reads its own list below, AFTER the removals.
    stale = [] if prune else landed_orphan_branches(repo, deep=False)
    if removable:
        print(f"WARN: {len(removable)} merged worktree(s) can be removed:")
        for tree, reason in removable:
            print(f"  {with_label(Path(tree.path).name, tree.path)} — {reason}")
    if stale and not prune:
        print(f"WARN: {len(stale)} orphan session branch(es) already on origin/master")
    if prune:
        # Unconditional, unlike the report above: the weekly timer runs this path for the
        # object-store repair at prune_all's tail, which must still happen on a week with
        # nothing to remove.
        failed = prune_all(
            repo,
            [tree for tree, _ in removable],
            reaper=reap_claims if reap else skip_reap,
        )
        # Only this path hashes orphans: the banner's 5s budget cannot pay for it.
        orphans = orphan_verdicts(repo)
        for path, unstored in orphans:
            if unstored:
                print(f"kept orphan {path}: {len(unstored)} file(s) git never stored")
        failed += prune_orphans([path for path, unstored in orphans if unstored == []])
        # Read AFTER the removals, because each one frees a branch: a list taken before them
        # names none of those, and the sweep would leave its own leavings for next week
        # instead of converging in one pass.
        failed += sweep_branches(repo, landed_orphan_branches(repo, deep=True))
        return FAILED if failed else OK
    if removable or stale:
        print("  → uv run python scripts/dev/prune_worktrees.py --prune")
    return OK


def check(branch: str) -> int:
    """Print one branch's landed/unlanded verdict and the layer that decided it.

    The worktree-cleanup skill's merged check. Comparing a `git merge-tree` SHA with master's
    tree SHA by eye is only the third of is_merged's four layers: a squash
    merge that master has since drifted into a conflict with reads unlanded there, and only
    the forge layer settles it.
    """
    repo = primary_checkout()
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return USAGE_ERROR
    head = git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{branch}^{{commit}}",
        cwd=repo,
        check=False,
    )
    if head.returncode != 0:
        print(f"no such branch or commit: {branch}", file=sys.stderr)
        return USAGE_ERROR
    layer = merged_layer(repo, head.stdout.strip(), branch)
    if layer is None:
        print(f"{branch}: unlanded")
        return FAILED
    print(f"{branch}: landed ({layer})")
    return OK


def main(argv: list[str] | None = None) -> int:
    """Report each session worktree's removable/keep/orphan verdict, and prune with `--prune`.

    Exits 1 when not inside a git repository, or when `--prune` left a removable worktree,
    orphan directory or branch in place; 0 otherwise.
    """
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "remove the worktrees reported as removable and delete the orphan session "
            "branches (worktree-* and claude/*) already on origin/master (default: report "
            "only)"
        ),
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help=(
            "shorten the report to one line per removable worktree, silent when clean; "
            "combines with --prune, which still removes them"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "in the long report, list the kept worktrees too, two lines each; with a bare "
            "--check, list the kept branches too"
        ),
    )
    parser.add_argument(
        "--no-reap",
        action="store_true",
        help=(
            "with --prune, skip the `findings.py reap` that follows a removal. A claim is "
            "judged against this checkout's worktrees alone, so a host that does not run the "
            "fan-out orchestrator reads every claim the orchestrator holds as stale"
        ),
    )
    parser.add_argument(
        "--gc",
        action="store_true",
        help=(
            "run the object-store repair alone and exit: prune unreachable objects and clear "
            "a stale gc.log, without touching any worktree"
        ),
    )
    parser.add_argument(
        "--check",
        metavar="BRANCH",
        nargs="?",
        const="",
        help=(
            "alone, print what the branch sweep would delete and why, and count the session "
            "branches it keeps under each reason. With BRANCH, print whether its work is already on origin/master, and the layer that "
            "decided it (ancestry, patch-id, content or forge); exit 0 landed, 1 unlanded, "
            "64 for an unknown branch. Reads only"
        ),
    )
    args = parser.parse_args(argv)

    if args.check is not None:
        return check(args.check) if args.check else check_sweep(args.all)

    if args.gc:
        repo = primary_checkout()
        if repo is None:
            print("not inside a git repository", file=sys.stderr)
            return 1
        for line in repair_object_store(repo):
            print(line)
        return 0

    if args.brief:
        return brief(prune=args.prune, reap=not args.no_reap)

    repo = primary_checkout()
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return 1

    orphans = []
    for path, unstored in orphan_verdicts(repo):
        for line in orphan_report(path, unstored):
            print(line)
        if unstored == []:
            orphans.append(path)

    # The long report names each branch, so it pays the deep containment check whether or not
    # it is about to delete anything — a report that disagreed with what `--prune` then removed
    # would be worse than a slow one.
    stale = landed_orphan_branches(repo, deep=True)
    for branch in stale:
        print(
            f"[{STALE:9}] {branch}\n            no worktree, content already on origin/master"
        )

    report, removable, vanished = survey_report(survey(repo), args.all)
    for line in report:
        print(line)

    # A vanished registration alone is still work: prune_all's repair_object_store runs the
    # `git worktree prune` that drops it, which VANISHED's reason promises `--prune` does.
    if not removable and not stale and not vanished and not orphans:
        print("\nnothing to remove")
        return 0

    if not args.prune:
        print(
            f"\n{len(removable)} worktree(s), {len(orphans)} orphan dir(s), {len(stale)} "
            f"branch(es) and {vanished} vanished registration(s) removable — re-run with "
            "--prune to remove"
        )
        return 0

    failed = prune_orphans(orphans)
    failed += prune_all(
        repo, removable, reaper=skip_reap if args.no_reap else reap_claims
    )
    # Re-read for brief()'s reason: the removals above just freed a branch each.
    failed += sweep_branches(repo, landed_orphan_branches(repo, deep=True))
    return FAILED if failed else OK


if __name__ == "__main__":
    sys.exit(main())
