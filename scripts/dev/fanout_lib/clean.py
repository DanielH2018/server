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
# `scripts/lib` sits two levels up from this package, one above the `scripts/dev` insert
# above; `lib.git` / `lib.gh` are the one way this tree runs git and gh (issue #2136).
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.manifest import Batch
from fanout_lib.transport import REPO
from lib.git import git
from prune_worktrees import REMOVABLE, Worktree, classify, is_dirty, is_merged, remove


def unlock(repo: str, path: str) -> None:
    """Unlock `path` in `repo`'s worktree admin, ignoring the git call's own exit status.

    A tree that is already unlocked makes this a harmless no-op.
    """
    git("worktree", "unlock", path, cwd=repo, check=False)


def lock(repo: str, path: str, reason: str) -> None:
    """Lock `path` in `repo`'s worktree admin with `reason`, ignoring the git call's exit status."""
    git("worktree", "lock", "--reason", reason, path, cwd=repo, check=False)


def delete_branch(repo: str, branch: str) -> tuple[bool, str]:
    """Force-delete `branch` in `repo`, returning `(ok, git's first stderr line)`.

    `-D`, not `-d`: a squash- or rebase-landed branch is never an ancestor of master, and
    the caller only reaches this for a branch `is_merged` has already settled.
    """
    deleted = git("branch", "-D", branch, cwd=repo, check=False)
    if deleted.returncode == 0:
        return True, ""
    first_line = next(
        (ln for ln in deleted.stderr.splitlines() if ln.strip()),
        f"git branch -D exited {deleted.returncode}",
    )
    return False, first_line.strip()


def _drop_merged_branch(repo: str, tree: Worktree, merged: bool, brancher) -> str:
    """Delete a removed tree's branch when it landed; return the `kept` reason, or `""`.

    Removing a worktree leaves its branch, so without this every cleaned batch left a
    `worktree-fanout-<batch>` branch for the operator to delete by hand (#2674).
    """
    if not (tree.branch and merged):
        return ""
    ok, err = brancher(repo, tree.branch)
    return "" if ok else f"— branch {tree.branch} not deleted: {err}"


def clean_one(
    repo: str,
    tree: Worktree,
    ask=is_merged,
    dirty=is_dirty,
    remover=remove,
    unlocker=unlock,
    locker=lock,
    brancher=delete_branch,
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
        brancher: `delete_branch`-shaped — deletes the landed branch once its tree is gone.

    Returns:
        `("removed" | "kept", reason)`. A tree removed whose branch then refuses to delete
        reads `kept`, naming the branch, so `cmd_clean` keeps the manifest pointing at it.
    """
    unlocked = dataclasses.replace(tree, locked=False, lock_reason="")
    merged = ask(repo, tree.head, tree.branch or "")
    try:
        tree_is_dirty = dirty(tree.path)
    except FileNotFoundError:
        return _clean_missing_tree(repo, tree, merged, remover, brancher)
    verdict, reason = classify(unlocked, merged=merged, dirty=tree_is_dirty)
    if verdict != REMOVABLE:
        return "kept", reason
    if tree.locked:
        unlocker(repo, tree.path)
    ok, err = remover(repo, unlocked)
    if ok:
        branch_err = _drop_merged_branch(repo, tree, merged, brancher)
        return ("kept", branch_err) if branch_err else ("removed", "")
    if tree.locked:
        locker(repo, tree.path, tree.lock_reason)
    return "kept", err


def _clean_missing_tree(
    repo: str, tree: Worktree, merged: bool, remover, brancher
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
    branch_err = _drop_merged_branch(repo, tree, merged, brancher)
    return ("kept", branch_err) if branch_err else ("removed", "(already gone)")


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


def remote_clean_command(b: Batch, repo: str = REPO) -> str:
    """The command `clean` runs on `b.host` to clean up one batch.

    Refuses while `b.unit` is still active, before anything else runs (#1872). On the
    2026-09-17 fan-out a `clean` run to clear one batch's failed launch also removed two
    batches whose units were still running: their trees were clean and at master, so the
    merge check judged them landed, and the units carried on against a deleted cwd. An
    active unit is a batch still working, so it reads `kept:` — the verdict that exits 0 and
    tells the operator to come back — naming the unit and `stop` as the way through. A
    `systemctl` that cannot reach the bus exits non-zero here and the chain proceeds as it
    did before the check existed; the local leg's bus is pinned in `transport.local_env`.

    Resets `b.unit` next (Ruling 11): the unit runs without `--collect`, so a failed run
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

    "Absent" is decided by the `.git` file every linked worktree carries, not by the
    directory (#1948). An agent that removes its own worktree on exit leaves a `.remember/`
    stub at the same path — a plugin hook writes it into the session's cwd after the tree
    is gone — so the directory exists while the checkout does not, and a `-e` test on the
    path sent the chain into the interpreter leg with the same exit 2. Both legs run this
    one chain, so the local/remote split the issue described was never the difference:
    the daniel-server batches converged because their directories were fully gone. The
    stub is removed BEFORE the registration, and not only because `launch` refuses a
    batch whose path exists and a batch id recurs across runs: `git worktree remove`
    accepts a registered path whose directory is missing, but refuses one whose directory
    is present and not a checkout, and a registration that survives keeps `branch -D`
    refusing — the executing test reads `kept: … not deleted` with the order the other way.
    `rm -rf` here only ever reaches a path that is not a checkout: the `.git` test that
    gates the branch is what makes it safe.

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

    The stale registration goes FIRST, before anything looks at the branch. Git refuses
    `branch -D` for a branch a registered worktree still holds, and a `rm -rf`'d directory
    leaves that registration behind — so deleting the branch first could never work. It
    printed `kept: … not deleted` for every merged gone-tree batch, and `cmd_clean` reads
    `kept` as a tree to come back to once the PR merges, which it already had. Only an
    executing test finds this: the chain's shape was asserted for months while this ordering
    was wrong (issue #1677).

    Deregistering is by path, naming this batch's own worktree, which replaces Ruling 37's
    repo-global `git worktree prune` per the same issue. Ruling 37's safety claim held —
    prune drops only registrations whose directory is missing and skips locked ones, so it
    could not take another session's live tree. The complaint is breadth, not correctness: a
    batch clean has no business deregistering trees no batch of this run created. `git
    worktree remove` accepts a path whose directory is already gone (the same behaviour
    `_clean_missing_tree` relies on), so it does the same job scoped to one path. Its
    failure is swallowed: a second pass over the same batch finds nothing left to
    deregister and must not stop the chain. The `worktree unlock` ahead of it releases this
    batch's own launch lock, which `remove` otherwise refuses on.

    Args:
        b: the batch to clean, as recorded in the run manifest.
        repo: the checkout the chain acts on. A seam, and a load-bearing one: it is what
            lets a test EXECUTE this chain against a scratch repo instead of the shared
            primary checkout, where `branch -D` and `worktree remove` would hit whatever
            every other live session is doing.
    """
    wt, branch = b.worktree, b.branch
    gone_branch = f'echo "removed: {wt} (already gone)"'
    return (
        f"if systemctl --user is-active --quiet {b.unit}; then "
        f'echo "kept: {wt} — unit {b.unit} still active; stop it first"; '
        f"else "
        f"systemctl --user reset-failed {b.unit} 2>/dev/null; "
        f"git -C {repo} fetch --quiet origin master && "
        f"if [ ! -e {wt}/.git ]; then "
        f"rm -rf {wt}; "
        f"git -C {repo} worktree unlock {wt} 2>/dev/null; "
        f"git -C {repo} worktree remove --force {wt} 2>/dev/null || true; "
        f"if ! git -C {repo} show-ref --verify --quiet refs/heads/{branch}; then "
        f"{gone_branch}; "
        f"else tip=$(git -C {repo} rev-parse refs/heads/{branch} 2>/dev/null); "
        f"merged=$(cd {repo} && gh pr list --state merged --head {branch} "
        f"--json headRefOid --jq '.[].headRefOid' 2>/dev/null "
        f'| grep -c -x "${{tip:-none}}"); '
        f"case \"${{merged}}\" in ''|*[!0-9]*) merged=0;; esac; "
        f'if [ "${{merged}}" -gt 0 ]; then '
        f"git -C {repo} branch -D {branch} >/dev/null 2>&1 && {gone_branch} "
        f'|| echo "kept: {wt} — branch {branch} not deleted"; '
        f'else echo "kept: {wt} — branch {branch} unmerged, tree gone"; '
        f"fi; "
        f"fi; "
        f"else cd {repo} && uv run --no-project --no-python-downloads --python 3.14.6 "
        f"python {wt}/scripts/dev/fanout_place.py clean-one {wt} {branch}; "
        f"fi; "
        f"fi"
    )
