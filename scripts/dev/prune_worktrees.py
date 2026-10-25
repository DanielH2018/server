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

An orphan directory, one under .claude/worktrees/ that git no longer registers, is removed
too when it holds nothing git lacks. One that holds content git never stored is kept, and the
report names each such file. See dev.worktree_orphans.
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
from lib.worktree_label import with_label

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
    the weekly `--prune --brief` down with it (#4191). It is never REMOVABLE either: there is
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
) -> int:
    """Remove each tree, printing one line per outcome.

    Shared by both report shapes so that `--prune` cannot mean one thing with `--brief` and
    another without it. A failed removal goes to stderr, followed by `advise`'s lines, which
    name any path the removal could not delete because another uid owns it.

    `remover` is `lib.worktrees.remove` unless a test hands it another, read at call time so
    a test of `main` that replaces this module's `remove` still reaches it.

    Returns:
      How many of `trees` were left in place. A tree a live process still uses counts too:
      the survey called it removable, so keeping it is a removal that did not happen.
    """
    remover = remove if remover is None else remover
    removed = failed = 0
    for tree in trees:
        ok, error = remover(repo, tree)
        if ok:
            removed += 1
            print(f"removed {tree.path}")
        else:
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


def brief(prune: bool = False) -> int:
    """One line per removable worktree; silent when there is nothing to remove.

    Returns 0, or with `prune` 1 when any removal or branch deletion failed.

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
        print(f"WARN: {len(removable)} merged worktree(s) can be removed:")
        for tree, reason in removable:
            print(f"  {with_label(Path(tree.path).name, tree.path)} — {reason}")
    if stale and not prune:
        print(
            f"WARN: {len(stale)} orphan {BRANCH_PREFIX}* branch(es) already on "
            "origin/master"
        )
    if prune:
        # Unconditional, unlike the report above: the weekly cron runs this path for the
        # object-store repair at prune_all's tail, which must still happen on a week with
        # nothing to remove.
        failed = prune_all(repo, [tree for tree, _ in removable])
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

    Exits 1 when not inside a git repository, or when `--prune` left a removable worktree,
    orphan directory or branch in place; 0 otherwise.
    """
    parser = ArgumentParser(description=__doc__)
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

    removable = []
    vanished = 0
    for verdict, tree, reason in survey(repo):
        print(f"[{verdict:9}] {with_label(tree.path, tree.path)}\n            {reason}")
        if verdict == REMOVABLE:
            removable.append(tree)
        vanished += verdict == VANISHED

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
    failed += prune_all(repo, removable)
    # Re-read for brief()'s reason: the removals above just freed a branch each.
    failed += sweep_branches(repo, landed_orphan_branches(repo, deep=True))
    return FAILED if failed else OK


if __name__ == "__main__":
    sys.exit(main())
