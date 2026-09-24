#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree. Nothing removed
them when the work merged, so merged trees accumulated on disk alongside the live ones and
it stopped being obvious which was which.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.

It also sweeps the BRANCHES those worktrees leave behind. Removing a worktree leaves its
`worktree-*` branch, and nothing here removed those: the dotfiles `prune-worktrees.py`
SessionStart hook did until its PR #626 (2026-09-24) made it skip any repo shipping this
script. There were roughly 249 of them on that date. See orphan_branches.

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

`--brief` chooses the report's shape and nothing else. It is orthogonal to `--prune`:
`--prune --brief` removes the same worktrees `--prune` alone would, and prints a short
report of what it removed.

A prune also repairs the shared object store the removed worktrees leave litter in, and
`--gc` runs that repair alone. See repair_object_store.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git import git, git_dirty, git_stdout, repair_object_store
from lib.repo_paths import REPO

# The readers — the Worktree record, the porcelain parser, the lock-liveness check, the
# cherry and merge-tree verdicts — are shared with the dotfiles prune-worktrees.py hook
# through the deployed claude_worktree module (#2133); `_claude_worktree` is the
# bootstrap onto it and says why a missing deploy raises rather than falls back. The
# names are re-exported: `findings_lib`, `fanout_lib` and the SessionStart banner import
# them from here, and `parse_worktree_list`/`session_is_alive` are this module's public
# reading of a worktree whichever file defines them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _claude_worktree  # noqa: F401
from claude_worktree import (
    Worktree,
    cherry_says_landed,
    forge_says_merged,
    merge_tree_says_contained,
    parse_worktree_list,
    session_is_alive,
)

REMOVABLE = "removable"
KEEP = "keep"
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


def classify(tree: Worktree, merged: bool, dirty: bool) -> tuple[str, str]:
    """Return (verdict, reason) for one worktree.

    Reasons are reported in priority order so the output names the blocking condition a
    person would act on first, rather than listing every condition that happens to fail.
    """
    if tree.locked and session_is_alive(tree.lock_reason):
        return KEEP, f"in use — {tree.lock_reason or 'locked'}"
    if dirty:
        return KEEP, "uncommitted changes"
    if tree.branch is None:
        return KEEP, "detached HEAD — no branch to check"
    if not merged:
        return KEEP, f"{tree.branch} not merged into origin/master"
    if tree.locked:
        return REMOVABLE, f"{tree.branch} merged, clean, lock owner is dead"
    return REMOVABLE, f"{tree.branch} merged, clean, unlocked"


def _git(args: list[str], cwd: str | None = None) -> str:
    return git_stdout(*args, cwd=cwd, check=False)


def is_merged(repo: str, head: str, branch: str = "") -> bool:
    """True when `head`'s work is already on origin/master, by ancestry or by patch.

    Ancestry alone misses how PRs actually land here. `gh pr merge --rebase` replays the
    commits onto master as new objects, so the branch tip is never an ancestor of
    origin/master and a merge-base check calls every landed worktree unmerged — which is why
    the pruner reported "nothing to remove" indefinitely while merged trees piled up. The
    ancestry check is kept because it is cheap and settles the fast-forward and merge-commit
    cases; `git cherry` compares by patch-id and settles the rebase case.

    Squash merges defeat both: several commits collapse into one, so the tip is not an
    ancestor and no patch-id matches either. `git merge-tree` settles that case by asking a
    different question — not "are these commits upstream" but "does this branch still have
    anything to give master". See merge_tree_says_contained.

    `git merge-tree` in turn fails on a squash-merged branch once master has DRIFTED into a
    conflict on a file the branch also touched: it exits non-zero, which is the right local
    answer (no verdict) and the wrong final one, so the tree sits there forever. The fourth
    layer asks the forge which head SHA it actually merged. It runs last because it is the only
    one needing a network round-trip and credentials.

    All four failures are closed: an unknown reads as NOT merged, because this decides what
    to DELETE.
    """
    if locally_landed(repo, head):
        return True
    # Fourth and last: squash-merged AND master has since drifted into a conflict on a file the
    # branch also touched. `git merge-tree` then exits non-zero, which is the right local answer
    # ("no verdict") and the wrong final one — the branch landed days ago and the tree sits there
    # forever. Observed 2026-08-27: worktree-review-2026-08-24-remediation, landed as PR #400 on
    # 2026-08-24, held by a later master change to wg-easy/tasks/main.yml.
    #
    # Ask the forge, which knows what it merged. This runs LAST because it is the only check
    # needing a network round-trip and credentials; every branch the local checks settle never
    # reaches it. No `gh`, no auth, or no answer all mean no verdict, which reads as not merged.
    # The lookup and its SHA-equality rule live in the deployed claude_worktree module, which
    # the dotfiles pruner and Stop hook also use (dotfiles #629).
    return forge_says_merged(repo, branch, head)


def locally_landed(repo: str, head: str, ancestry_known: bool = False) -> bool:
    """The first three layers of is_merged — the ones that cost no network round-trip.

    Split out for the branch sweep, which asks this question of every orphan `worktree-*`
    branch and must not reach the forge to answer it. `is_merged`'s fourth layer is one
    `gh pr list` per unsettled branch; the memo on `_memoised_merged` records what that class
    of traffic already cost once (#1279), and a sweep over a couple of hundred branches is the
    same bug at a larger N.

    `ancestry_known` says the caller has already settled the ancestry layer in bulk, through
    `ancestry_landed_branches`, so this skips the per-branch `merge-base` call.
    """
    if not ancestry_known:
        ancestor = git(
            "merge-base", "--is-ancestor", head, "origin/master", cwd=repo, check=False
        )
        if ancestor.returncode == 0:
            return True
    cherry = git("cherry", "origin/master", head, cwd=repo, check=False)
    # A failed `git cherry` prints nothing, and empty output otherwise means "merged" — so
    # the return code has to gate this, or an unknown ref would read as safe to delete.
    if cherry.returncode != 0:
        return False
    if cherry_says_landed(cherry.stdout, empty_means=True):
        return True
    master_tree = git("rev-parse", "origin/master^{tree}", cwd=repo, check=False)
    if master_tree.returncode != 0:
        return False
    # Exit is non-zero on a conflict, and on a git too old for --write-tree (added in 2.38).
    # Both mean "no verdict", which must read as not merged.
    merged_tree = git(
        "merge-tree", "--write-tree", "origin/master", head, cwd=repo, check=False
    )
    return merged_tree.returncode == 0 and merge_tree_says_contained(
        merged_tree.stdout, master_tree.stdout
    )


BRANCH_PREFIX = "worktree-"


def orphan_branches(repo: str) -> list[str]:
    """Local `worktree-*` branches that no worktree has checked out.

    `EnterWorktree` derives a branch from the worktree name, so every session branch here
    carries the prefix and nothing else does. Removing the worktree leaves the branch, and
    nothing swept those: the dotfiles `prune-worktrees.py` SessionStart hook did until its
    PR #626 (2026-09-24) made it skip any repo shipping this script, and it only ever accepted
    what `git branch -d` accepted. On 2026-09-24 there were roughly 249 such branches here.

    The prefix is the outer refusal: a branch outside it is not a session branch and this
    never touches it. A branch a worktree still holds is excluded too, which is also what
    makes the current branch safe — the primary checkout is a worktree in this list.
    """
    refs = _git(
        ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}*"],
        cwd=repo,
    ).split()
    held = {
        tree.branch
        for tree in parse_worktree_list(
            _git(["worktree", "list", "--porcelain"], cwd=repo)
        )
        if tree.branch
    }
    return [ref for ref in refs if ref not in held]


def ancestry_landed_branches(repo: str) -> set[str]:
    """Every local branch whose tip is an ancestor of origin/master, in ONE git call.

    The bulk layer, and the reason the sweep is cheap enough to run from the SessionStart
    banner. A per-branch `merge-base --is-ancestor` over a couple of hundred branches is a
    couple of hundred processes; `git branch --merged` is one, and the issue's own count says
    it settles most of them (172 of 249 on 2026-09-24).

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


def is_dirty(path: str) -> bool:
    # Untracked counted: an unlanded scratch file in a worktree is exactly the thing that must
    # stop it being pruned. lib.git.git_dirty makes that scope explicit at the call site.
    return git_dirty(path, include_untracked=True)


def remove(repo: str, tree: Worktree) -> tuple[bool, str]:
    """Unlock if needed, then remove.

    Never --force: git's own refusal on a tree with uncommitted or untracked files is the backstop
    that makes auto-unlock safe here — classify() only marks a locked tree REMOVABLE once
    session_is_alive() has confirmed the owner is dead, so this never releases a lock a live session
    still holds. Without the unlock, `git worktree remove` fails outright on a locked tree ("cannot
    remove a locked working tree") and the whole prune silently no-ops while reporting the tree as
    removed.
    """
    if tree.locked:
        git("worktree", "unlock", tree.path, cwd=repo, check=False)
    result = git("worktree", "remove", tree.path, cwd=repo, check=False)
    return result.returncode == 0, result.stderr.strip()


def primary_checkout() -> str | None:
    """The checkout the worktrees hang off, or None when we are not in a git repo.

    Not the current one: worktrees live under the primary's .claude/worktrees/, and
    --show-toplevel run from inside a worktree returns the worktree itself, which made the
    orphan scan look in a directory that doesn't exist.
    """
    common_dir = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return str(Path(common_dir).parent) if common_dir else None


def _memoised_merged(
    repo: str, ask: Callable[[str, str, str], bool] = is_merged
) -> Callable[[Worktree], bool]:
    """`is_merged` for a worktree, answered once per tree and cached for the rest of the call.

    A fan-out claims every issue under ONE orchestrator worktree, so `claim_states` asks the
    identical question once per claimed issue — and for an unmerged branch `is_merged` is four
    layers deep, ending in a `gh pr list` NETWORK call. N claimed issues meant N of those on
    every `claims`, `reap` and `next` (#1279); the memory entry
    `agent-polling-starves-the-deployers-ci-gate` records what that class of traffic costs.

    The memo is keyed on the worktree PATH, whose answer is stable for the duration of one
    command. It deliberately does NOT touch `classify`: an earlier attempt made the read lazy
    by guarding on `not tree.locked`, which re-derived `classify`'s branch structure outside
    `classify` and got it wrong — `classify` skips the merged read only on `locked AND
    session_is_alive`. `classify` still receives an eagerly-computed bool.

    ``ask`` is the read itself, defaulted to `is_merged`. A test counts its calls through
    this parameter rather than by patching the module attribute, which is the direction the
    monkeypatch ratchet in `ansible/tests/repo/` pushes callers.
    """
    cache: dict[str, bool] = {}

    def merged(tree: Worktree) -> bool:
        if tree.path not in cache:
            cache[tree.path] = ask(repo, tree.head, tree.branch or "")
        return cache[tree.path]

    return merged


def _worktree_facts() -> tuple[
    list[Worktree], Callable[[str], bool], Callable[[Worktree], bool], bool
]:
    """(worktrees, dirty, merged, ok): the staleness inputs, plus whether the read worked.

    `ok` is False only when the git call itself failed, never merely because it found no
    worktrees — `findings.py`'s `reap` needs that distinction to avoid releasing every claim
    in the register on a transient git error: `git worktree list --porcelain` run with
    `check=False` returns an empty string on failure, which `parse_worktree_list` reads as
    zero worktrees, which makes every claim read as "no worktree — stale".

    Separated so a caller replaces one attribute rather than patching three modules, and so
    `findings.py`'s `claims` and `reap` handlers share exactly one definition of the facts.
    """
    repo = primary_checkout() or str(REPO)
    result = git("worktree", "list", "--porcelain", cwd=repo, check=False)
    if result.returncode != 0:
        return [], is_dirty, _memoised_merged(repo), False
    trees = parse_worktree_list(result.stdout)
    return trees, is_dirty, _memoised_merged(repo), True


def survey(repo: str) -> list[tuple[str, Worktree, str]]:
    """(verdict, worktree, reason) for every session worktree, primary excluded."""
    trees = parse_worktree_list(_git(["worktree", "list", "--porcelain"], cwd=repo))
    out = []
    for tree in trees[1:]:
        verdict, reason = classify(
            tree,
            merged=is_merged(repo, tree.head, tree.branch or ""),
            dirty=is_dirty(tree.path),
        )
        out.append((verdict, tree, reason))
    return out


def prune_all(repo: str, trees: list[Worktree]) -> None:
    """Remove each tree, printing one line per outcome.

    Shared by both report shapes so that `--prune` cannot mean one thing with `--brief` and
    another without it. It used to live inline in main(), below an early `return brief()`,
    so `--prune --brief` printed the removable list and removed nothing while exiting 0 —
    a silent no-op that read as a successful prune (#1190).
    """
    for tree in trees:
        ok, error = remove(repo, tree)
        if ok:
            print(f"removed {tree.path}")
        else:
            print(f"could not remove {tree.path}: {error}")
    for line in repair_object_store(repo):
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
    args = parser.parse_args(argv)

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
            _git(["worktree", "list", "--porcelain"], cwd=repo)
        )
    }
    for path in find_orphan_dirs(str(Path(repo) / ".claude" / "worktrees"), tracked):
        print(
            f"[{ORPHAN:9}] {path}\n            git does not track this — remove by hand"
        )

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
