"""Remove a finished batch's worktree, by prune_worktrees' content check — spec §4.

The reverse state of launch. Never removes a dirty or unmerged tree, and names what it keeps.
"""

import dataclasses
import subprocess

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting. `parents[1]` is
# THIS file's own `scripts/dev` — when the remote leg runs a worktree's own copy of
# fanout_place.py (Ruling E), that resolves to the worktree's `scripts/dev`, so `clean-one`
# imports the worktree's OWN prune_worktrees rather than the primary checkout's stale one.
# Never change this to an absolute path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import Batch
from fanout_lib.transport import REPO
from prune_worktrees import REMOVABLE, Worktree, classify, is_dirty, is_merged, remove


def unlock(repo: str, path: str) -> None:
    """Unlock `path` in `repo`'s worktree admin, ignoring the git call's own exit status.

    A tree that is already unlocked makes this a harmless no-op.
    """
    subprocess.run(
        ["git", "-C", repo, "worktree", "unlock", path],
        capture_output=True,
        check=False,
    )


def lock(repo: str, path: str, reason: str) -> None:
    """Lock `path` in `repo`'s worktree admin with `reason`, ignoring the git call's exit status."""
    subprocess.run(
        ["git", "-C", repo, "worktree", "lock", "--reason", reason, path],
        capture_output=True,
        check=False,
    )


def clean_one(
    repo: str,
    tree: Worktree,
    ask=is_merged,
    dirty=is_dirty,
    remover=remove,
    unlocker=unlock,
    locker=lock,
) -> tuple[str, str]:
    """Remove `tree` when its branch landed and it is clean; otherwise keep it and say why.

    `classify` reads a locked tree carrying an unrecognized lock reason (a `fanout-<batch>`
    launch lock, not a Claude session lock) as a live session and keeps it forever — Ruling
    13. This judges an in-memory unlocked copy so classify decides on merged/dirty state
    instead, but only touches the REAL lock when a tree is actually about to be removed: the
    lock is released right before `remover` runs, and put back with its original reason if
    `remover` then fails. Every KEEP verdict leaves the lock exactly as it found it — a batch
    that is still running or still unmerged is never unlocked at all.

    Args:
        repo: the checkout `tree` is registered under.
        tree: the worktree to evaluate, as read from `git worktree list --porcelain`.
        ask: `is_merged`-shaped — whether `tree`'s branch already landed.
        dirty: `is_dirty`-shaped — whether `tree` has uncommitted or untracked changes.
        remover: `prune_worktrees.remove`-shaped — removes a worktree.
        unlocker: unlocks `tree` right before an actual removal.
        locker: re-locks `tree` with its original reason when a removal attempt fails.

    Returns:
        `("removed" | "kept", reason)`.
    """
    unlocked = dataclasses.replace(tree, locked=False, lock_reason="")
    merged = ask(repo, tree.head, tree.branch or "")
    verdict, reason = classify(unlocked, merged=merged, dirty=dirty(tree.path))
    if verdict != REMOVABLE:
        return "kept", reason
    if tree.locked:
        unlocker(repo, tree.path)
    ok, err = remover(repo, unlocked)
    if ok:
        return "removed", ""
    if tree.locked:
        locker(repo, tree.path, tree.lock_reason)
    return "kept", err


def remote_clean_command(b: Batch) -> str:
    """The command `clean` runs on `b.host` to clean up one batch.

    Resets `b.unit` first (Ruling 11): the unit runs without `--collect`, so a failed run
    lingers in the user manager and a relaunch of the same batch id dies with "unit already
    exists". A `;` separates it from the rest, since a unit that never failed makes this a
    harmless error rather than something that should stop the clean.

    Then fetches before anything reads merge state: `b.worktree` shares refs with the
    primary checkout on `b.host`, and daniel-server's primary checkout is stale by design —
    every local merge check in `prune_worktrees.is_merged` (ancestry, patch-id, merge-tree)
    would judge against a stale `origin/master` there, leaving only the network `gh pr list`
    fallback able to say "merged" at all. A failed fetch stops the command (`&&`) rather than
    silently falling through to that stale state.

    The `clean-one` leg runs the fan-out worktree's OWN copy of this script
    (`<b.worktree>/scripts/dev/fanout_place.py`), not the primary checkout's, because that
    checkout is stale by design and the fresh worktree is the only checkout there guaranteed
    to carry this code (Ruling E). It runs from the primary checkout's cwd (`cd {REPO}`) so a
    relative path elsewhere in the command still resolves there, and under the same pinned,
    project-free interpreter as `HEALTH_CMD` (`--no-project --no-python-downloads --python
    3.14.6`) — every import in `clean-one`'s own chain is stdlib, so a stale primary whose
    lock file fails to sync with the worktree's `uv.lock` can't take the clean down with it.
    """
    return (
        f"systemctl --user reset-failed {b.unit} 2>/dev/null; "
        f"git -C {REPO} fetch --quiet origin master && "
        f"cd {REPO} && uv run --no-project --no-python-downloads --python 3.14.6 python "
        f"{b.worktree}/scripts/dev/fanout_place.py clean-one {b.worktree} {b.branch}"
    )
