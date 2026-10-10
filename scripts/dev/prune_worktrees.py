#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.
The removal itself then refuses a tree that a live process still uses as its cwd or its
`CLAUDE_PROJECT_DIR`, naming the pid, so the report can say `removable` for a tree that
`--prune` then keeps. See lib.worktrees.processes_using.

It also sweeps the BRANCHES those worktrees leave behind. See orphan_branches.

"Merged" is checked three ways, cheapest first: ancestry, then patch-id, then content. PRs
land here rebased or squashed, never fast-forwarded, so the branch tip is not an ancestor of
origin/master — on ancestry alone this script reported "nothing to remove" while merged trees
piled up. `git cherry` compares by patch-id and settles the rebase case. A squash defeats
both, because collapsing several commits into one leaves no patch-id to match; `git merge-tree`
settles that by asking whether merging the branch would change master at all. See is_merged.

The lock is the interesting one. Claude Code locks a session's worktree with a reason
naming the owning process — `claude session <name> (pid 1285937 start 2164388)` — and does
not release it when the session ends. Treating any lock as "in use" would therefore keep
every abandoned worktree forever. The `start` field is the process start time from
/proc/<pid>/stat, so it distinguishes a live owner from a dead one whose pid has since been
reused, and a lock whose owner is gone is ignored rather than obeyed.

Usage:
    uv run python scripts/dev/prune_worktrees.py            # report only (default)
    uv run python scripts/dev/prune_worktrees.py --prune    # also remove the removable ones
    uv run python scripts/dev/prune_worktrees.py --brief    # short report, for a banner
    uv run python scripts/dev/prune_worktrees.py --gc       # object-store repair only
    uv run python scripts/dev/prune_worktrees.py --check <branch>   # one branch's verdict

`--check` answers "does this branch still hold unlanded work" for one branch, with the same
four-layer ladder the pruner deletes by. It prints `landed (<layer>)` or `unlanded` and exits
0 or 1; an unknown branch, or no repository, exits 64. It reads only, and removes nothing.

`--brief` chooses the report's shape and nothing else. It is orthogonal to `--prune`:
`--prune --brief` removes the same worktrees `--prune` alone would, and prints a short
report of what it removed.

A prune also repairs the shared object store the removed worktrees leave litter in, and
`--gc` runs that repair alone. See repair_object_store.
"""

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev.foreign_owned import foreign_owned_advice
from lib.exit_codes import FAILED, OK, USAGE_ERROR
from lib.git import git, git_stdout, repair_object_store

# The worktree library: the readers, `is_merged` and `classify`. This file is only the CLI
# over it, and nothing else imports this file -- every other consumer imports `lib.worktrees`.
from lib.worktrees import (
    REMOVABLE,
    Worktree,
    classify,
    is_dirty,
    is_merged,
    locally_landed,
    merged_layer,
    parse_worktree_list,
    primary_checkout,
    remove,
)

ORPHAN = "orphan"
STALE = "stale"


def find_orphan_dirs(worktrees_dir: str, registered: set[str]) -> list[str]:
    """Directories under .claude/worktrees/ that git no longer tracks as worktrees.

    `git worktree prune` deregisters a tree whose administrative data is gone but leaves
    the directory behind, so these are invisible to `git worktree list` and to the removal
    path below — they have to be reported separately and deleted by hand.
    """
    try:
        entries = sorted(Path(worktrees_dir).iterdir())
    except OSError:
        return []
    return [str(e) for e in entries if e.is_dir() and str(e) not in registered]


BRANCH_PREFIX = "worktree-"


def orphan_branches(repo: str) -> list[str]:
    """Local `worktree-*` branches that no worktree has checked out.

    `EnterWorktree` derives a branch from the worktree name, so every session branch here
    carries the prefix and nothing else does.

    The prefix is the outer refusal: a branch outside it is not a session branch and this
    never touches it. A branch a worktree still holds is excluded too, which is also what
    makes the current branch safe — the primary checkout is a worktree in this list.
    """
    refs = git_stdout(
        "for-each-ref",
        "--format=%(refname:short)",
        f"refs/heads/{BRANCH_PREFIX}*",
        cwd=repo,
        check=False,
    ).split()
    held = {
        tree.branch
        for tree in parse_worktree_list(
            git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
        )
        if tree.branch
    }
    return [ref for ref in refs if ref not in held]


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


def landed_orphan_branches(repo: str, deep: bool) -> list[str]:
    """The orphan `worktree-*` branches whose content is already on origin/master.

    `deep` chooses how hard to look, and the two callers want different answers. The banner
    passes False and gets the bulk ancestry layer alone — two git calls, no per-branch work,
    and it UNDERCOUNTS a branch that landed by rebase or squash. That is the right trade for
    a line whose job is to say "there is something to sweep": `--brief` is budgeted at 5s by
    `.claude/hooks/session-health.py` and measured at 1.3s warm.

    `--prune` passes True and pays `git cherry` plus `git merge-tree` on the residue the bulk
    layer did not settle, because a deletion has to be right per branch rather than in
    aggregate. Neither path reaches the forge; see locally_landed.
    """
    ancestry = ancestry_landed_branches(repo)
    landed = []
    for branch in orphan_branches(repo):
        if branch in ancestry:
            landed.append(branch)
        elif deep and locally_landed(repo, branch, ancestry_known=True):
            landed.append(branch)
    return landed


def delete_branch(repo: str, branch: str) -> tuple[bool, str]:
    """Delete one orphan branch, `-d` first and `-D` only if that refuses.

    `git branch -d` accepts ancestry only, so it refuses every branch that landed by rebase or
    squash — which is most of them here, and is why the dotfiles hook swept so few. `-D` throws
    away git's own backstop, so the containment check in landed_orphan_branches IS the safety:
    this is never called for a branch a local layer has not already settled.
    """
    result = git("branch", "-d", branch, cwd=repo, check=False)
    if result.returncode == 0:
        return True, ""
    forced = git("branch", "-D", branch, cwd=repo, check=False)
    return forced.returncode == 0, forced.stderr.strip()


def sweep_branches(repo: str, branches: list[str]) -> None:
    """Delete each already-settled orphan branch, printing one line per outcome.

    Takes the list rather than deriving it, so the caller's report and its removals cannot
    name different branches.
    """
    for branch in branches:
        ok, error = delete_branch(repo, branch)
        print(f"deleted {branch}" if ok else f"could not delete {branch}: {error}")


def survey(repo: str) -> list[tuple[str, Worktree, str]]:
    """(verdict, worktree, reason) for every session worktree, primary excluded."""
    trees = parse_worktree_list(
        git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
    )
    out = []
    for tree in trees[1:]:
        verdict, reason = classify(
            tree,
            merged=is_merged(repo, tree.head, tree.branch or ""),
            dirty=is_dirty(tree.path),
        )
        out.append((verdict, tree, reason))
    return out


def reap_claims(repo: str) -> list[str]:
    """Run the checkout's own `findings.py reap` and return the lines to print.

    A removed worktree's claims read as stale from then on, but only `reap` releases them, and
    nothing ran it outside a fan-out's launch (#3928). It is the checkout's copy, so a scratch
    repo with no `scripts/dev/findings.py` reaps nothing and says so.
    """
    findings = Path(repo) / "scripts" / "dev" / "findings.py"
    if not findings.is_file():
        return [f"claims not reaped: no {findings}"]
    # This interpreter, not `uv`: the weekly cron's PATH omits ~/.local/bin, and it already
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


def prune_all(
    repo: str,
    trees: list[Worktree],
    advise: Callable[[str], list[str]] = foreign_owned_advice,
    remover: Callable[[str, Worktree], tuple[bool, str]] | None = None,
    reaper: Callable[[str], list[str]] = reap_claims,
) -> None:
    """Remove each tree, printing one line per outcome.

    Shared by both report shapes so that `--prune` cannot mean one thing with `--brief` and
    another without it. A failed removal is followed by `advise`'s lines, which name any
    path git could not delete because another uid owns it.

    `remover` is `lib.worktrees.remove` unless a test hands it another, read at call time so
    a test of `main` that replaces this module's `remove` still reaches it.
    """
    remover = remove if remover is None else remover
    removed = 0
    for tree in trees:
        ok, error = remover(repo, tree)
        if ok:
            removed += 1
            print(f"removed {tree.path}")
        else:
            print(f"could not remove {tree.path}: {error}")
            for line in advise(tree.path):
                print(line)
    for line in repair_object_store(repo):
        print(line)
    if removed:
        for line in reaper(repo):
            print(line)


def brief(prune: bool = False) -> int:
    """One line per removable worktree; silent when there is nothing to remove.

    This exists for the SessionStart banner, which prints nothing on a healthy day and has to
    stay cheap to read. Claude Code's own worktree keeper already lists every branch whose
    commits landed by squash or rebase, but each line ends by asking the reader to go run
    `gh pr list --state merged --head <branch>` themselves — the lookup is_merged() already
    performs. This prints the answer instead of the homework.

    # DECIDED: the banner REPORTS, the weekly cron PRUNES. Both halves of the sweep are
    # automatic, and neither runs from a SessionStart hook. `--prune` removes worktrees and
    # deletes branches in the PRIMARY checkout, which every concurrent session shares, and it
    # ends in `repair_object_store` — a `git gc` whose duration is unbounded against a hook
    # budgeted at 5s in `.claude/hooks/session-health.py`. Running it at every session start
    # would put N sessions' removals in a race for a saving one weekly run already gets. The
    # cron in `ansible/roles/setup/initial_setup/tasks/crons.yml` is the arm that removes, for
    # #1435's reason: a worktree-isolated session's git commands are refused against the
    # primary checkout, so the sessions that SEE the mess are the ones structurally unable to
    # clear it. Full reasoning in this docstring and at that cron.
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
        print(f"\U0001f9f9 {len(removable)} merged worktree(s) can be removed:")
        for tree, reason in removable:
            print(f"  {Path(tree.path).name} — {reason}")
    if stale and not prune:
        print(
            f"\U0001f9f9 {len(stale)} orphan {BRANCH_PREFIX}* branch(es) already on "
            "origin/master"
        )
    if prune:
        # Unconditional, unlike the report above: the weekly cron runs this path for the
        # object-store repair at prune_all's tail, which must still happen on a week with
        # nothing to remove.
        prune_all(repo, [tree for tree, _ in removable])
        # Read AFTER the removals, because each one frees a branch: a list taken before them
        # names none of those, and the sweep would leave its own leavings for next week
        # instead of converging in one pass.
        sweep_branches(repo, landed_orphan_branches(repo, deep=True))
    elif removable or stale:
        print("  → uv run python scripts/dev/prune_worktrees.py --prune")
    return 0


def check(branch: str) -> int:
    """Print one branch's landed/unlanded verdict and the layer that decided it.

    The worktree-cleanup skill's merged check. It used to compare a `git merge-tree` SHA with
    master's tree SHA by eye, which is only the third of is_merged's four layers: a squash
    merge that master has since drifted into a conflict with reads unlanded there, and only
    the forge layer settles it (#3929).
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

    Exits 1 when not inside a git repository, 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "remove the worktrees reported as removable and delete the orphan worktree-* "
            "branches already on origin/master (default: report only)"
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
        help=(
            "print whether BRANCH's work is already on origin/master, and the layer that "
            "decided it (ancestry, patch-id, content or forge); exit 0 landed, 1 unlanded, "
            "64 for an unknown branch. Reads only"
        ),
    )
    args = parser.parse_args(argv)

    if args.check:
        return check(args.check)

    if args.gc:
        repo = primary_checkout()
        if repo is None:
            print("not inside a git repository", file=sys.stderr)
            return 1
        for line in repair_object_store(repo):
            print(line)
        return 0

    if args.brief:
        return brief(prune=args.prune)

    repo = primary_checkout()
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return 1

    tracked = {
        t.path
        for t in parse_worktree_list(
            git_stdout("worktree", "list", "--porcelain", cwd=repo, check=False)
        )
    }
    for path in find_orphan_dirs(str(Path(repo) / ".claude" / "worktrees"), tracked):
        print(
            f"[{ORPHAN:9}] {path}\n            git does not track this — remove by hand"
        )
        # A removal that failed on a foreign-owned path leaves exactly this: git unregisters
        # the tree after the delete fails, so the remnant surfaces here on the next run.
        for line in foreign_owned_advice(path):
            print(line)

    # The long report names each branch, so it pays the deep containment check whether or not
    # it is about to delete anything — a report that disagreed with what `--prune` then removed
    # would be worse than a slow one.
    stale = landed_orphan_branches(repo, deep=True)
    for branch in stale:
        print(
            f"[{STALE:9}] {branch}\n            no worktree, content already on origin/master"
        )

    removable = []
    for verdict, tree, reason in survey(repo):
        print(f"[{verdict:9}] {tree.path}\n            {reason}")
        if verdict == REMOVABLE:
            removable.append(tree)

    if not removable and not stale:
        print("\nnothing to remove")
        return 0

    if not args.prune:
        print(
            f"\n{len(removable)} worktree(s) and {len(stale)} branch(es) removable — "
            "re-run with --prune to remove"
        )
        return 0

    prune_all(repo, removable)
    # Re-read for brief()'s reason: the removals above just freed a branch each.
    sweep_branches(repo, landed_orphan_branches(repo, deep=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
