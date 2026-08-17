#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree. Nothing removed
them when the work merged, so merged trees accumulated on disk alongside the live ones and
it stopped being obvious which was which.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.

"Merged" is checked by ancestry AND by patch-id. PRs land here with `gh pr merge --rebase`,
which replays commits onto master as new objects, so a landed branch is never an ancestor of
origin/master — on ancestry alone this script reported "nothing to remove" while merged trees
piled up. See is_merged.

The lock is the interesting one. Claude Code locks a session's worktree with a reason
naming the owning process — `claude session <name> (pid 1285937 start 2164388)` — and does
not release it when the session ends. Treating any lock as "in use" would therefore keep
every abandoned worktree forever. The `start` field is the process start time from
/proc/<pid>/stat, so it distinguishes a live owner from a dead one whose pid has since been
reused, and a lock whose owner is gone is ignored rather than obeyed.

Usage:
    uv run python scripts/prune_worktrees.py            # report only (default)
    uv run python scripts/prune_worktrees.py --prune    # also remove the removable ones
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REMOVABLE = "removable"
KEEP = "keep"
ORPHAN = "orphan"

LOCK_OWNER = re.compile(r"\(pid (\d+) start (\d+)\)")


@dataclass
class Worktree:
    path: str
    head: str
    branch: str | None
    locked: bool
    lock_reason: str = ""


def parse_worktree_list(porcelain: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into records, primary checkout first."""
    trees: list[Worktree] = []
    path = head = branch = None
    locked, reason = False, ""
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            head, branch, locked = None, None, False
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "locked" or line.startswith("locked "):
            locked = True
            reason = line[len("locked ") :] if line.startswith("locked ") else ""
        elif line == "" and path is not None:
            trees.append(Worktree(path, head or "", branch, locked, reason))
            path = head = branch = None
            locked, reason = False, ""
    if path is not None:
        trees.append(Worktree(path, head or "", branch, locked, reason))
    return trees


def session_is_alive(lock_reason: str) -> bool:
    """Is the process named in a worktree's lock reason still running?

    The reason Claude Code writes carries the owning pid and its start time, e.g.
    `claude session foo (pid 1285937 start 2164388)`. Comparing the start time against
    /proc/<pid>/stat rejects a pid that has been reused since the session died. A reason
    in any other format is treated as live: an unrecognized lock is someone else's, and
    guessing wrong destroys work.
    """
    match = LOCK_OWNER.search(lock_reason)
    if not match:
        return True
    pid, start = match.group(1), match.group(2)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The comm field can itself contain spaces and parentheses, so field numbering is only
    # reliable after the final ')'. starttime is field 22, the 20th of those that follow.
    fields = stat.rpartition(")")[2].split()
    return len(fields) > 19 and fields[19] == start


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
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def cherry_says_merged(cherry_output: str) -> bool:
    """Read `git cherry origin/master <head>` output: True when every commit is upstream.

    One line per commit on `head`, prefixed `-` when an equivalent patch is already on
    origin/master and `+` when it isn't. No lines means nothing is ahead of upstream, which
    is merged.
    """
    lines = [line for line in cherry_output.splitlines() if line.strip()]
    return all(line.startswith("-") for line in lines)


def is_merged(repo: str, head: str) -> bool:
    """True when `head`'s work is already on origin/master, by ancestry or by patch.

    Ancestry alone misses how PRs actually land here. `gh pr merge --rebase` replays the
    commits onto master as new objects, so the branch tip is never an ancestor of
    origin/master and a merge-base check calls every landed worktree unmerged — which is why
    the pruner reported "nothing to remove" indefinitely while merged trees piled up. The
    ancestry check is kept because it is cheap and settles the fast-forward and merge-commit
    cases; `git cherry` compares by patch-id and settles the rebase case.

    Squash merges remain undetected: several commits collapse into one, so no patch-id
    matches. Those worktrees are kept, which is the safe direction — this decides what to
    DELETE, so an unknown must never read as merged.
    """
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, "origin/master"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode == 0:
        return True
    cherry = subprocess.run(
        ["git", "cherry", "origin/master", head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    # A failed `git cherry` prints nothing, and empty output otherwise means "merged" — so
    # the return code has to gate this, or an unknown ref would read as safe to delete.
    if cherry.returncode != 0:
        return False
    return cherry_says_merged(cherry.stdout)


def is_dirty(path: str) -> bool:
    return bool(_git(["status", "--porcelain"], cwd=path))


def remove(repo: str, tree: Worktree) -> tuple[bool, str]:
    """Unlock if needed, then remove. Never --force: git's own refusal on a tree with
    uncommitted or untracked files is the backstop that makes auto-unlock safe here —
    classify() only marks a locked tree REMOVABLE once session_is_alive() has confirmed the
    owner is dead, so this never releases a lock a live session still holds. Without the
    unlock, `git worktree remove` fails outright on a locked tree ("cannot remove a locked
    working tree") and the whole prune silently no-ops while reporting the tree as removed.
    """
    if tree.locked:
        subprocess.run(
            ["git", "worktree", "unlock", tree.path],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    result = subprocess.run(
        ["git", "worktree", "remove", tree.path],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, result.stderr.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove the worktrees reported as removable (default: report only)",
    )
    args = parser.parse_args()

    # The primary checkout, not this one: worktrees live under the primary's
    # .claude/worktrees/, and --show-toplevel run from inside a worktree returns the
    # worktree itself, which made the orphan scan look in a directory that doesn't exist.
    common_dir = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not common_dir:
        print("not inside a git repository", file=sys.stderr)
        return 1
    repo = str(Path(common_dir).parent)

    trees = parse_worktree_list(_git(["worktree", "list", "--porcelain"], cwd=repo))
    # The first entry is the checkout the others hang off; it is not a session worktree.
    sessions = trees[1:]

    orphans = find_orphan_dirs(
        str(Path(repo) / ".claude" / "worktrees"), {t.path for t in trees}
    )
    for path in orphans:
        print(
            f"[{ORPHAN:9}] {path}\n            git does not track this — remove by hand"
        )

    if not sessions:
        print("no session worktrees")
        return 0

    removable = []
    for tree in sessions:
        verdict, reason = classify(
            tree, merged=is_merged(repo, tree.head), dirty=is_dirty(tree.path)
        )
        print(f"[{verdict:9}] {tree.path}\n            {reason}")
        if verdict == REMOVABLE:
            removable.append(tree)

    if not removable:
        print("\nnothing to remove")
        return 0

    if not args.prune:
        print(f"\n{len(removable)} removable — re-run with --prune to remove")
        return 0

    for tree in removable:
        ok, error = remove(repo, tree)
        if ok:
            print(f"removed {tree.path}")
        else:
            print(f"could not remove {tree.path}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
