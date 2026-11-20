"""Remove a finished batch's worktree, by lib.worktrees' content check — spec §4.

The reverse state of launch. Never removes a dirty or unmerged tree, and names what it keeps.
"""

import dataclasses
import shlex
import subprocess

from pathlib import Path as _Path


import time
from collections.abc import Callable
from datetime import UTC, datetime

from dev.fanout_lib import manifest as manifest_mod
from dev.fanout_lib import status as status_mod
from dev.fanout_lib.launch import archive_command
from dev.fanout_lib.manifest import Batch, Manifest
from dev.fanout_lib.target import PROJECT_DIR_ENV, SERVER_TARGET, Target, resolve
from dev.fanout_lib.transport import REPO, Tools
from lib.git import git
from lib.worktrees import (
    REMOVABLE,
    WORKTREE_HOLDERS_SOCKET,
    Worktree,
    classify,
    escape_holder,
    is_dirty,
    is_merged,
    remove,
)


def _first_line(proc, what: str) -> str:
    """git's first non-empty stderr line, or `what` and its exit status.

    `git()` captures text, so `.stderr` is a str on both a `CompletedProcess` and a
    `CalledProcessError`. A git that failed silently still has to say something, and `what`
    is what it says — the failing command, so the fallback names it.
    """
    return next(
        (ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()),
        f"{what} exited {proc.returncode}",
    )


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
    return False, _first_line(deleted, "git branch -D")


def _drop_merged_branch(repo: str, tree: Worktree, merged: bool, brancher) -> str:
    """Delete a removed tree's branch when it landed; return the `kept` reason, or `""`.

    Removing a worktree leaves its branch, so without this every cleaned batch left a
    batch branch (`target.branch_name`) for the operator to delete by hand.
    """
    if not (tree.branch and merged):
        return ""
    ok, err = brancher(repo, tree.branch)
    return "" if ok else f"— branch {tree.branch} not deleted: {err}"


PARKED_KEPT = (
    "— parked with its PR unmerged: the tree holds the batch's claims and the PR holds its "
    "edits, so re-land it first"
)


def is_parked(worktree: str) -> bool:
    """Whether the batch in `worktree` ended `parked`, read from the report it left there."""
    try:
        report = (_Path(worktree) / ".fanout" / "report.json").read_text()
    except OSError:
        return False
    return status_mod.final_state(report) == status_mod.PARKED


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

    A batch that ended `parked` with its PR unmerged is kept whatever else holds, and the
    reason names the re-land: its claims expire with the worktree, and the open PR still
    holds the edits. A batch parked after its merge is judged like any merged one.

    `classify` reads a `fanout-<batch>` launch lock as live for a day after launch, and while
    the batch's unit is active (`lib.fanout_lock`). The caller has already checked the unit,
    so this judges an in-memory unlocked copy and classify decides on merged/dirty state
    instead — Ruling 13. It only touches the REAL lock when a tree is actually about to be removed: the
    lock is released right before `remover` runs, and put back with its original reason if
    `remover` then fails. Every KEEP verdict leaves the lock exactly as it found it — a batch
    that is still running or still unmerged is never unlocked at all.

    A directory `rm -rf`'d by hand rather than through the normal removal path stays
    registered (`prunable`, per `git worktree list --porcelain`'s own label) with nothing
    left to read — `dirty(tree.path)` raises `FileNotFoundError` trying to run git in a cwd
    that no longer exists — so that specific failure hands the tree to
    `_clean_missing_tree` instead of propagating.

    The two ways `dirty` fails mean opposite things, so they are answered differently. A
    `FileNotFoundError` says the cwd itself is gone, which is the goal state — that tree is
    handed to `_clean_missing_tree` to deregister. A `CalledProcessError` says the directory
    is there and git refuses to read it, which a worktree carrying a submodule whose `.git`
    file points at a missing gitdir does. Unreadable is not clean: the tree is kept,
    naming git's own first line, so the operator reads why instead of a truncated traceback.

    Args:
        repo: the checkout `tree` is registered under.
        tree: the worktree to evaluate, as read from `git worktree list --porcelain`.
        ask: `is_merged`-shaped — whether `tree`'s branch already landed.
        dirty: `is_dirty`-shaped — whether `tree` has uncommitted or untracked changes.
        remover: `lib.worktrees.remove`-shaped — removes a worktree.
        unlocker: unlocks `tree` right before an actual removal.
        locker: re-locks `tree` with its original reason when a removal attempt fails.
        brancher: `delete_branch`-shaped — deletes the landed branch once its tree is gone.

    Returns:
        `("removed" | "kept", reason)`. A tree removed whose branch then refuses to delete
        reads `kept`, naming the branch, so `cmd_clean` keeps the manifest pointing at it.
    """
    unlocked = dataclasses.replace(tree, locked=False, lock_reason="")
    merged = ask(repo, tree.head, tree.branch or "")
    if not merged and is_parked(tree.path):
        return "kept", PARKED_KEPT
    try:
        tree_is_dirty = dirty(tree.path)
    except FileNotFoundError:
        return _clean_missing_tree(repo, tree, merged, remover, brancher)
    except subprocess.CalledProcessError as exc:
        return "kept", f"— git status failed: {_first_line(exc, 'git status')}"
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
    It must not report `removed: … (already gone)` regardless: that tells the operator the
    branch is gone while it is still there, and `cmd_clean` deletes the run manifest on that
    word, leaving nothing pointing at what survived. `kept` keeps the manifest.

    Returns:
        `("removed", "(already gone)")` once the registration and any merged branch are
        gone, or `("kept", "— branch <name> not deleted: <git's first stderr line>")`.
    """
    ok, err = remover(repo, tree)
    if not ok:
        return "kept", err
    branch_err = _drop_merged_branch(repo, tree, merged, brancher)
    return ("kept", branch_err) if branch_err else ("removed", "(already gone)")


_TRACEBACK_HEADER = "Traceback (most recent call last):"


def read_clean_result(proc: subprocess.CompletedProcess) -> tuple[str, str]:
    """Judge one remote clean leg: what happened, and the line to print for it.

    A leg that never reached a verdict is a transport or environment failure, not a tree
    that must stay — ssh refused, the fetch failed, `uv` missing. Reading those as `kept`
    told the operator to re-run once the PR merged, when the PR had merged and the host was
    the problem, and `clean` exited 0 on it.

    Args:
        proc: the finished remote call.

    A failed leg is reported by its first non-empty stderr line, except when stderr holds a
    Python traceback. There the first line is the `Traceback (most recent call
    last):` header, which names nothing, so the last non-empty line is reported instead:
    that is the exception type and message. The check is for the header anywhere, not only
    first, because `uv` can print a warning ahead of the interpreter's output.

    Returns:
        `("removed" | "kept" | "failed", line)`. The verdict comes from the output's own
        prefix, so a `kept:` verdict stays `kept` whatever the exit status: the leg spoke.
    """
    line = (proc.stdout or "").strip()
    for verdict in ("removed", "kept"):
        if line.startswith(f"{verdict}:"):
            return verdict, line
    errs = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
    if any(ln.startswith(_TRACEBACK_HEADER) for ln in errs):
        detail = errs[-1]
    else:
        detail = errs[0] if errs else line or "no output"
    return "failed", f"clean failed (exit {proc.returncode}): {detail}"


# What `live_process_scan`'s awk does with the root helper's answer. It reads the framing
# `lib.worktrees.read_answer` reads, and prints `unknown (...)` for any answer that function
# refuses, so the chain keeps the tree. `WT` is the worktree escaped the helper's way: awk
# compares the escaped forms and never decodes. It arrives through ENVIRON, because
# `awk -v` would read its backslashes as escapes.
_HOLDER_AWK = (
    'NR == 1 { if ($0 != "ok") { bad = 1; if ($1 == "refused") refused = 1 }; next } '
    "ended { bad = 1; next } "
    '$0 == "end" { ended = 1; next } '
    "NF != 3 || $1 !~ /^[0-9]+$/ "
    '|| ($2 != "cwd" && $2 != "CLAUDE_PROJECT_DIR" && $2 != "unreadable") '
    "{ bad = 1; next } "
    'hit == "" && ($2 == "unreadable" || $3 == ENVIRON["WT"] '
    '|| index($3, ENVIRON["WT"] "/") == 1) { hit = $1 } '
    'END { if (refused) print "unknown (worktree-holders refused this uid)"; '
    'else if (bad || !ended) print "unknown (worktree-holders answered garbled)"; '
    "else print hit }"
)


def live_process_scan(
    wt: str, sock: str = WORKTREE_HOLDERS_SOCKET, proc_root: str = "/proc"
) -> str:
    """Shell that sets `busy` to the first pid whose cwd or `CLAUDE_PROJECT_DIR` is in `wt`.

    `lib.worktrees.processes_using` in shell, for the chains that run before or without an
    interpreter (Ruling 30): `remove` refuses a tree a live process uses, and a chain
    that deletes the tree itself skipped that check. `busy` is empty when nothing
    uses the tree.

    The root helper `initial_setup` installs answers first, through its socket, when this
    uid may connect, so another uid's process outside this login slice is seen. The
    socket needs no setuid, so a claude-rc.service shell, which runs with `NoNewPrivs`, asks
    it too. `nc -U` is the client, since a shell has no unix-socket redirect. As in
    `privileged_holders`, it fails closed once the socket is there: a failed connection, a
    refusal, an answer without its framing, an `unreadable` line or a line of an unknown kind
    sets `busy`. A socket that is absent, or that this uid's groups cannot write, skips the
    helper with a warning on stderr. The helper reports only under the worktree root
    `lib.worktrees.holder_root` names for the caller, so the `/proc` loop still runs when it
    found nothing, for a tree outside that root. That loop cannot read another uid's process
    and skips it.

    Args:
        wt: the worktree path.
        sock: the helper's socket. A test seam.
        proc_root: the directory the `/proc` loop walks, one entry per pid. A test seam: the
            loop forks `readlink`, `tr` and `grep` per pid, so a test that needs only the
            chain around the scan points this at an empty directory rather than pay for the
            host's process count.
    """
    escaped = shlex.quote(escape_holder(wt))
    return (
        "busy=; "
        f'if [ -S "{sock}" ] && [ -w "{sock}" ]; then '
        f'if out=$(nc -U "{sock}" </dev/null 2>/dev/null); then '
        f"busy=$(printf '%s\\n' \"$out\" | WT={escaped} awk -F '\\t' '{_HOLDER_AWK}'); "
        'else busy="unknown (worktree-holders did not answer)"; fi; '
        f"else echo \"warning: cannot connect to {sock}, so another uid's process outside "
        'this login slice is invisible to the in-use check (#4170)" >&2; fi; '
        'if [ -z "$busy" ]; then '
        f"for p in {proc_root}/[0-9]*; do "
        'c=$(readlink "$p/cwd" 2>/dev/null); '
        f'case "$c" in "{wt}"|"{wt}"/*) busy=${{p#{proc_root}/}}; break;; esac; '
        'if tr "\\0" "\\n" < "$p/environ" 2>/dev/null '
        f'| grep -q -F -x -e "CLAUDE_PROJECT_DIR={wt}" '
        f'|| tr "\\0" "\\n" < "$p/environ" 2>/dev/null '
        f'| grep -q -F -e "CLAUDE_PROJECT_DIR={wt}/"; '
        f"then busy=${{p#{proc_root}/}}; break; fi; done; fi; "
    )


def busy_refusal(wt: str) -> str:
    """The `kept:` line a chain prints when `live_process_scan` found a pid using `wt`."""
    return f'echo "kept: {wt} — pid $busy still uses it"'


# What the `clean-one` leg's snapshot holds. `clean-one` imports across `scripts/dev`,
# `scripts/lib` and `scripts/deploy_tools`, so the whole import root goes rather than a list
# of its packages that the next import would outgrow.
CLEAN_SNAPSHOT_PATHS = ("scripts",)


def remote_clean_command(
    b: Batch,
    repo: str | None = None,
    target: Target = SERVER_TARGET,
    server: str = REPO,
) -> str:
    """The command `clean` runs on `b.host` to clean up one batch.

    Refuses while `b.unit` is still active, before anything else runs. A `clean` run to clear
    one batch's failed launch must not also remove batches whose units are still running:
    their trees are clean and at master, so the merge check judges them landed, and the units
    carry on against a deleted cwd. An active unit is a batch still working, so it reads
    `kept:` — the verdict that exits 0 and tells the operator to come back — naming the unit
    and `stop` as the way through. A `systemctl` that cannot reach the bus exits non-zero here
    and the chain proceeds as it did before the check existed; the local leg's bus is pinned
    in `transport.local_env`.

    Resets `b.unit` next (Ruling 11): the unit runs without `--collect`, so a failed run
    lingers in the user manager and a relaunch of the same batch id dies with "unit already
    exists". A `;` separates it from the rest, since a unit that never failed makes this a
    harmless error rather than something that should stop the clean.

    Then fetches before anything reads merge state: `b.worktree` shares refs with the
    primary checkout on `b.host`, and daniel-server's primary checkout is stale by design —
    every local merge check in `lib.worktrees.is_merged` (ancestry, patch-id, merge-tree)
    would judge against a stale `origin/master` there, leaving only the network `gh pr list`
    fallback able to say "merged" at all. A failed fetch stops the command (`&&`) rather than
    silently falling through to that stale state.

    The `clean-one` leg runs a snapshot of `origin/master`'s `scripts/`, on every host and
    for every repo. The chain archives it from this repo's object store on `b.host` into a
    temporary directory, runs `scripts/dev/fanout.py place` from there, and removes the
    directory whatever the leg's exit status, which the chain then returns. `git archive`
    reads the object store, so no checkout's work tree changes. Two other copies of the script
    are on the host, and neither is current:

    - The batch worktree's copy would freeze the merge judgement at whatever the batch was
      launched from. A batch started before `landed` existed would run a `clean-one` that has
      no `landed`, so every squash-merged batch would read "not merged into origin/master"
      for as long as it stood.
    - The primary checkout's work tree tracks master on daniel-box only, where
      `homelab-checkout-sync` fast-forwards it. daniel-server has no such job, and its
      checkout stood 584 commits behind on 2026-11-19.

    The snapshot is as current as the fetch the chain has already run. For a batch of this
    repo that fetch is the one above. For a batch of another repo the chain fetches this
    repo's master as well, in the same call.

    A copy of the code outside any checkout cannot find the checkout from its own path, so
    the leg names it in `RUN_HOOK_PROJECT_DIR`, which `target.server_checkout` reads. The
    session that builds this chain may be older than the snapshot it runs, so `clean-one`
    keeps accepting the arguments this chain passes. A failed archive stops the leg before
    the interpreter starts and reads `failed`, naming git's or tar's first line, rather than
    keeping the tree silently. The leg runs from the checkout's cwd (`cd {repo}`) so a
    relative path elsewhere in the command still resolves there, and under the same pinned,
    project-free interpreter as `fanout_lib.session_banner.HEALTH_CMD` (`--no-project
    --no-python-downloads --python 3.15.2`): every import in `clean-one`'s own chain is
    stdlib, so the leg needs no venv and no lock file.

    The absent-tree case is answered in shell BEFORE the interpreter is needed (Ruling 30).
    The first pass deletes the worktree's own script, so an interpreter leg running it on the
    second pass would hit a missing path, exit 2, read the batch as `kept`, and never delete
    the run manifest. The shell answer also drops the stale registration and the merged branch, which `clean-one` does not.

    "Absent" is decided by the `.git` file every linked worktree carries, not by the
    directory. An agent that removes its own worktree on exit leaves a `.remember/`
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

    The merged test is `gh pr list --state merged --head <branch> --json headRefOid`, the same
    forge oracle `lib.worktrees.is_merged` ends on, because this repo squash-merges: a
    squashed branch is never an ancestor of `origin/master`, so `merge-base --is-ancestor`
    calls every landed branch unmerged. Ancestry exits 1 for a branch that landed by squash or
    rebase (Ruling 36).

    It matches on the head SHA, never on "a merged PR exists under this branch name" (Ruling
    38). Branch names are reused here — one branch can land several PRs, each with a different
    tip — and a fan-out batch id is per-run, so a name match would delete a branch holding
    work that never landed. `show-ref` has already found the local branch by the time this
    runs, so `rev-parse` has a tip to compare. The count is forced to 0 unless it is all
    digits, so a `gh` that is missing, unauthenticated or offline reads as not merged and the
    branch survives with a `kept:` line.

    A tip that differs from the merged head still counts when GitHub's compare calls it an
    ancestor of that head (`ahead` or `identical`): `land.sh` merges master into a
    stale-base branch on the remote, so the merged head is the local tip plus a merge commit.
    Every local commit is then in the merged PR. A diverged tip, a tip never pushed, or a failed
    `gh` keeps the branch.

    The stale registration goes FIRST, before anything looks at the branch. Git refuses
    `branch -D` for a branch a registered worktree still holds, and a `rm -rf`'d directory
    leaves that registration behind — so deleting the branch first could never work. It
    printed `kept: … not deleted` for every merged gone-tree batch, and `cmd_clean` reads
    `kept` as a tree to come back to once the PR merges, which it already had. Only an
    executing test finds an ordering error here: the chain's shape can be asserted while the
    ordering is wrong.

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

    A batch in another repo differs in three places. The chain acts on that repo's checkout,
    fetches its own default branch, and passes `--repo` to the snapshot's script, since the
    batch's worktree carries no copy at all.

    Args:
        b: the batch to clean, as recorded in the run manifest.
        repo: the checkout the chain acts on, defaulting to `target`'s. A seam, and a
            load-bearing one: it is what lets a test EXECUTE this chain against a scratch
            repo instead of the shared primary checkout, where `branch -D` and `worktree
            remove` would hit whatever every other live session is doing.
        target: the repo the batch works, which names its default branch.
        server: this repo's checkout on `b.host`, whose `origin/master` the leg's snapshot
            is archived from. A seam for the same reason as `repo`: a test that executes the
            chain points it at a scratch checkout.
    """
    wt, branch = b.worktree, b.branch
    repo = repo or target.checkout
    snapshot = '"$snap"/server'
    # DECIDED: the `clean-one` leg runs a snapshot of `origin/master`'s `scripts/` that this
    # chain archives on the batch's host. Not the batch worktree's copy, which is frozen at the
    # batch's launch, and not the primary checkout's work tree, which only daniel-box keeps at
    # master. Full reasoning in this function's docstring.
    clean_one_leg = (
        "snap=$(mktemp -d) && { "
        + (
            ""
            if server == repo
            else f"git -C {server} fetch --quiet origin {SERVER_TARGET.base_branch} && "
        )
        + f"{archive_command(server, snapshot, CLEAN_SNAPSHOT_PATHS)} && "
        f"cd {repo} && {PROJECT_DIR_ENV}={server} PYTHONPATH={snapshot}/scripts "
        f"uv run --no-project --no-python-downloads --python 3.15.2 "
        f"python {snapshot}/scripts/dev/fanout.py place clean-one {wt} {branch}"
        + ("" if target.is_server else f" --repo {target.repo}")
        + '; rc=$?; rm -rf "$snap"; (exit $rc); }'
    )
    gone_branch = f'echo "removed: {wt} (already gone)"'
    # DECIDED: the gone-tree leg keeps its own `gh pr list` merged check in shell rather than
    # calling `lib.worktrees.is_merged`. The interpreter leg starts only for a tree that is
    # still a checkout, and a gone tree has no cwd for it to judge (Ruling 30). The shell
    # answers before an interpreter is needed. It asks only the forge question because a
    # squash-merged branch fails every local layer.
    # Full reasoning in this function's docstring.
    return (
        f"if systemctl --user is-active --quiet {b.unit}; then "
        f'echo "kept: {wt} — unit {b.unit} still active; stop it first"; '
        f"else "
        f"systemctl --user reset-failed {b.unit} 2>/dev/null; "
        f"git -C {repo} fetch --quiet origin {target.base_branch} && "
        f"if [ ! -e {wt}/.git ]; then "
        f"{live_process_scan(wt)}"
        f'if [ -n "$busy" ]; then {busy_refusal(wt)}; else '
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
        f'if [ "${{merged}}" -eq 0 ] && [ -n "${{tip}}" ]; then '
        f"for h in $(cd {repo} && gh pr list --state merged --head {branch} "
        f"--json headRefOid --jq '.[].headRefOid' 2>/dev/null); do "
        f"st=$(cd {repo} && gh api "
        f'"repos/{{owner}}/{{repo}}/compare/${{tip}}...${{h}}" --jq .status 2>/dev/null); '
        f'case "${{st}}" in ahead|identical) merged=1;; esac; '
        f"done; fi; "
        f'if [ "${{merged}}" -gt 0 ]; then '
        f"git -C {repo} branch -D {branch} >/dev/null 2>&1 && {gone_branch} "
        f'|| echo "kept: {wt} — branch {branch} not deleted"; '
        f'else echo "kept: {wt} — branch {branch} unmerged, tree gone"; '
        f"fi; "
        f"fi; "
        f"fi; "
        f"else {clean_one_leg}; "
        f"fi; "
        f"fi"
    )


def clean_run(
    run: Manifest,
    tools: Tools,
    root: _Path,
    out: Callable[[str], None] = print,
    wanted: Callable[[Batch], bool] = lambda b: True,
) -> tuple[list[str], list[str]]:
    """Clean every standing batch of `run` that `wanted` admits, recording each removal.

    A removal is recorded rather than re-derived because the evidence is destroyed by the
    act: the remote leg reads the worktree, and the worktree is what it deletes. So the
    manifest is saved after each `removed:` verdict, a batch already carrying `removed_at`
    is skipped without an ssh call, and the run is retired only once every batch carries one.
    A batch `wanted` refuses is passed over silently and keeps the run live.

    A leg that reached no verdict at all — ssh refused, the fetch failed, a timeout — is
    failed, never kept: kept tells the operator to wait for a merge, failed to look at the
    host.

    Args:
        run: the loaded manifest.
        tools: the transport each leg runs over.
        root: the manifest directory `run` was loaded from and is saved back to.
        out: where each batch's line goes.
        wanted: asked right before each leg whether to run it.

    Returns:
        `(kept, failed)`, the batch ids of each.
    """
    kept, failed = [], []
    for i, b in enumerate(run.batches):
        if b.removed_at:
            out(f"{b.batch} on {b.host}: removed earlier ({b.removed_at})")
            continue
        if not wanted(b):
            continue
        try:
            target = resolve(b.repo, tools.default_ref)
        except ValueError as exc:
            out(f"{b.batch} on {b.host}: clean failed: {exc}")
            failed.append(b.batch)
            continue
        try:
            proc = tools.run(
                b.host, remote_clean_command(b, target=target), 120.0, None
            )
        except subprocess.TimeoutExpired:
            out(f"{b.batch} on {b.host}: clean timed out")
            failed.append(b.batch)
            continue
        verdict, line = read_clean_result(proc)
        out(f"{b.batch} on {b.host}: {line}")
        if verdict == "removed":
            run.batches[i] = dataclasses.replace(
                b, removed_at=datetime.now(UTC).isoformat()
            )
            manifest_mod.save(run, root=root)
        elif verdict == "failed":
            failed.append(b.batch)
        else:
            kept.append(b.batch)
    if all(b.removed_at for b in run.batches):
        manifest_mod.retire(run.run_id, root)
    return kept, failed


# How long one `sweep_runs` pass may spend on legs. The lander that calls it has
# `TimeoutStartSec=30min` for its landings' starts as well, and one leg can take 120s.
SWEEP_BUDGET_S = 15 * 60


def sweep_runs(
    tools: Tools,
    open_branches: frozenset[str],
    out: Callable[[str], None],
    root: _Path = manifest_mod.MANIFEST_DIR,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """`clean_run` over every live run under `root`, newest first, within `SWEEP_BUDGET_S`.

    The unattended reverse of `launch`: nothing else cleans a landed batch unless an
    operator runs `place clean`. Each leg keeps every safety `clean` has, so a batch whose unit
    is active, whose tree is dirty or whose branch is unmerged reads `kept`. A batch whose
    branch still heads an open PR is passed over without a leg, since it cannot be merged
    yet. Newest first, so a batch that just landed is reached before the budget runs out on
    old runs that keep their trees.

    Nothing here raises for one run's failure: a run whose manifest cannot be written logs a
    line and the sweep moves on, because the caller's exit status pages the operator.
    """
    deadline = clock() + SWEEP_BUDGET_S

    def wanted(b: Batch) -> bool:
        return b.branch not in open_branches and clock() < deadline

    for run in reversed(manifest_mod.all_runs(root)):
        if clock() >= deadline:
            out(f"clean sweep: out of time at run {run.run_id}; the next pass resumes")
            return

        def say(line: str, run_id: str = run.run_id) -> None:
            out(f"clean {run_id}: {line}")

        try:
            clean_run(run, tools, root, out=say, wanted=wanted)
        except OSError as exc:
            say(f"manifest not written: {exc}")
