#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree. Nothing removed
them when the work merged, so merged trees accumulated on disk alongside the live ones and
it stopped being obvious which was which.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.

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
import json
import sys
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.gh import gh
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
    merge_tree_says_contained,
    parse_worktree_list,
    session_is_alive,
)

REMOVABLE = "removable"
KEEP = "keep"
ORPHAN = "orphan"


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
    if merged_tree.returncode == 0 and merge_tree_says_contained(
        merged_tree.stdout, master_tree.stdout
    ):
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
    if not branch:
        return False
    pr_list = gh(
        "pr",
        "list",
        "--state",
        "merged",
        "--head",
        branch,
        "--json",
        "headRefOid",
        cwd=repo,
        check=False,
    )
    if pr_list.returncode != 0:
        return False
    return pr_head_says_merged(pr_list.stdout, head)


def pr_head_says_merged(stdout: str, head: str) -> bool:
    """Read `gh pr list --state merged --head <branch> --json headRefOid`.

    True when one of those merged PRs was merged from exactly this commit.

    Matching on the head SHA, never on "a merged PR exists for this branch name". Branch names are
    reused here — one session landed three PRs from `worktree-pi-detached-container-arm` on
    2026-08-27, each with a different tip — so a name match would delete a branch carrying work that
    never landed. SHA equality is the whole guarantee.
    """
    try:
        prs = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(prs, list):
        return False
    return any(isinstance(p, dict) and p.get("headRefOid") == head for p in prs)


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
    """
    repo = primary_checkout()
    if repo is None:
        return 0
    removable = [
        (tree, reason) for verdict, tree, reason in survey(repo) if verdict == REMOVABLE
    ]
    if not removable:
        return 0
    print(f"\U0001f9f9 {len(removable)} merged worktree(s) can be removed:")
    for tree, reason in removable:
        print(f"  {Path(tree.path).name} — {reason}")
    if prune:
        prune_all(repo, [tree for tree, _ in removable])
    else:
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
        help="remove the worktrees reported as removable (default: report only)",
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

    surveyed = survey(repo)
    if not surveyed:
        print("no session worktrees")
        return 0

    removable = []
    for verdict, tree, reason in surveyed:
        print(f"[{verdict:9}] {tree.path}\n            {reason}")
        if verdict == REMOVABLE:
            removable.append(tree)

    if not removable:
        print("\nnothing to remove")
        return 0

    if not args.prune:
        print(f"\n{len(removable)} removable — re-run with --prune to remove")
        return 0

    prune_all(repo, removable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
