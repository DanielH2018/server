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

    A directory `rm -rf`'d by hand rather than through the normal removal path stays
    registered (`prunable`, per `git worktree list --porcelain`'s own label) with nothing
    left to read — `dirty(tree.path)` raises `FileNotFoundError` trying to run git in a cwd
    that no longer exists — so that specific failure hands the tree to
    `_clean_missing_tree` instead of propagating.

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
    try:
        tree_is_dirty = dirty(tree.path)
    except FileNotFoundError:
        return _clean_missing_tree(repo, tree, merged, remover)
    verdict, reason = classify(unlocked, merged=merged, dirty=tree_is_dirty)
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


def _clean_missing_tree(
    repo: str, tree: Worktree, merged: bool, remover
) -> tuple[str, str]:
    """Deregister a worktree whose directory is already gone from disk.

    There is nothing left there to protect — clean or dirty, locked or not — so this prunes
    the stale registration unconditionally through the same `remover` seam (confirmed
    empirically: `git worktree remove` accepts a path whose directory no longer exists,
    without needing `--force`). The branch is a separate question: it may still hold commits
    this worktree never landed, so it is dropped only when `merged` says it already landed.

    A branch delete that FAILS reports `kept`, naming the branch and git's own first line.
    It used to report `removed: … (already gone)` regardless, which told the operator the
    branch was gone while it was still there — and `cmd_clean` deleted the run manifest on
    that word, leaving nothing pointing at what survived. `kept` keeps the manifest.

    Returns:
        `("removed", "(already gone)")` once the registration and any merged branch are
        gone, or `("kept", "— branch <name> not deleted: <git's first stderr line>")`.
    """
    ok, err = remover(repo, tree)
    if not ok:
        return "kept", err
    if tree.branch and merged:
        deleted = subprocess.run(
            ["git", "-C", repo, "branch", "-D", tree.branch],
            capture_output=True,
            text=True,
            check=False,
        )
        if deleted.returncode != 0:
            first_line = next(
                (ln for ln in deleted.stderr.splitlines() if ln.strip()),
                f"git branch -D exited {deleted.returncode}",
            )
            return "kept", f"— branch {tree.branch} not deleted: {first_line.strip()}"
    return "removed", "(already gone)"


def read_clean_result(proc: subprocess.CompletedProcess) -> tuple[str, str]:
    """Judge one remote clean leg: what happened, and the line to print for it.

    A leg that never reached a verdict is a transport or environment failure, not a tree
    that must stay — ssh refused, the fetch failed, `uv` missing. Reading those as `kept`
    told the operator to re-run once the PR merged, when the PR had merged and the host was
    the problem, and `clean` exited 0 on it.

    Args:
        proc: the finished remote call.

    Returns:
        `("removed" | "kept" | "failed", line)`. The verdict comes from the output's own
        prefix, so a `kept:` verdict stays `kept` whatever the exit status: the leg spoke.
    """
    line = (proc.stdout or "").strip()
    for verdict in ("removed", "kept"):
        if line.startswith(f"{verdict}:"):
            return verdict, line
    detail = next(
        (ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()),
        line or "no output",
    )
    return "failed", f"clean failed (exit {proc.returncode}): {detail}"


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

    That copy goes with the worktree the first pass deletes, so the absent-tree case is
    answered in shell BEFORE the interpreter is needed (Ruling 30). Without this the second
    pass ran a script path that no longer exists, python exited 2, the batch read `kept`,
    and the run manifest was never deleted — the re-run-once-merged flow `clean` itself
    prints could not converge.

    The absent-tree branch never prints `removed:` while the branch survives, which is
    Ruling 23's contract: a gone branch is `removed:`, a merged one is deleted first and
    only then `removed:`, and an unmerged one is `kept:`.

    The merged test is `gh pr list --state merged --head <branch> --json headRefOid`, the
    same forge oracle `prune_worktrees.is_merged` ends on, because this repo squash-merges: a
    squashed branch is never an ancestor of `origin/master`, so `merge-base --is-ancestor`
    calls every landed branch unmerged. Measured against two branches that really did land —
    the slice-1 work as PR #1484 and slice-2 as #1495 — ancestry exits 1 for both (Ruling 36).

    It matches on the head SHA, never on "a merged PR exists under this branch name" (Ruling
    38). Branch names are reused here — one session landed three PRs from
    `worktree-pi-detached-container-arm` on 2026-08-27, each with a different tip — and a
    fan-out batch id is per-run, so a name match would delete a branch holding work that
    never landed. `show-ref` has already found the local branch by the time this runs, so
    `rev-parse` has a tip to compare. The count is forced to 0 unless it is all digits, so a
    `gh` that is missing, unauthenticated or offline reads as not merged and the branch
    survives with a `kept:` line.

    Whatever the branch outcome, the stale registration goes: `git worktree prune` drops only
    registrations whose directory is missing, and skips a locked one, so it cannot touch
    another session's live tree (Ruling 37). The `worktree unlock` before it releases this
    batch's own launch lock, which would otherwise make prune skip this very tree.
    """
    wt, branch = b.worktree, b.branch
    gone_branch = f'echo "removed: {wt} (already gone)"'
    return (
        f"systemctl --user reset-failed {b.unit} 2>/dev/null; "
        f"git -C {REPO} fetch --quiet origin master && "
        f"if [ ! -e {wt} ]; then "
        f"if ! git -C {REPO} show-ref --verify --quiet refs/heads/{branch}; then "
        f"{gone_branch}; "
        f"else tip=$(git -C {REPO} rev-parse refs/heads/{branch} 2>/dev/null); "
        f"merged=$(cd {REPO} && gh pr list --state merged --head {branch} "
        f"--json headRefOid --jq '.[].headRefOid' 2>/dev/null "
        f'| grep -c -x "${{tip:-none}}"); '
        f"case \"${{merged}}\" in ''|*[!0-9]*) merged=0;; esac; "
        f'if [ "${{merged}}" -gt 0 ]; then '
        f"git -C {REPO} branch -D {branch} >/dev/null 2>&1 && {gone_branch} "
        f'|| echo "kept: {wt} — branch {branch} not deleted"; '
        f'else echo "kept: {wt} — branch {branch} unmerged, tree gone"; '
        f"fi; "
        f"fi; "
        f"git -C {REPO} worktree unlock {wt} 2>/dev/null; "
        f"git -C {REPO} worktree prune; "
        f"else cd {REPO} && uv run --no-project --no-python-downloads --python 3.14.6 "
        f"python {wt}/scripts/dev/fanout_place.py clean-one {wt} {branch}; "
        f"fi"
    )
