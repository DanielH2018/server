"""Abandon a fan-out batch whose branch never merged: `fanout_place.py abandon <run-id> <batch>`.

`clean` keeps an unmerged tree by design, so giving up on a batch took a hand-run teardown on
its host: unlock, force-remove, `branch -D`, a second `clean`, then one `findings.py release`
per issue (#3919, #3924). This is that teardown as one command. It discards whatever the agent
committed and stops the batch's agent, which is why it names exactly one batch.
"""

import dataclasses
import shlex
import subprocess
import sys
from datetime import UTC, datetime

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib import manifest as manifest_mod
from fanout_lib.clean import read_clean_result
from fanout_lib.manifest import Batch, Manifest
from fanout_lib.target import SERVER_TARGET, Target, resolve
from fanout_lib.transport import Tools


def remote_abandon_command(
    b: Batch, repo: str | None = None, target: Target = SERVER_TARGET
) -> str:
    """The command `abandon` runs on `b.host` to discard one batch's tree and branch.

    Stops `b.unit` first, since abandoning a batch means giving up on its agent, and an agent
    left running would go on writing into a tree being deleted. A unit still active after the
    stop reads `kept:`, and nothing is removed.

    The order is what git enforces. `worktree remove` refuses a locked tree, and `launch`
    locked it, so the unlock comes first. `branch -D` refuses a branch a registered worktree
    still holds, so the branch goes last. A tree with no `.git` file is a stub an exiting
    agent left behind, not a checkout, so it is removed with `rm -rf` and its registration
    dropped by path, the same as `clean`'s gone-tree leg.

    Prints exactly one line: `removed: <wt> (abandoned)` once neither the tree nor the branch
    is left, else `kept: <wt> — <what survived>`.

    Args:
        b: the batch to abandon, as recorded in the run manifest.
        repo: the checkout the chain acts on, defaulting to `target`'s. A test seam that keeps
            the chain off the shared primary checkout.
        target: the repo the batch works.
    """
    wt, branch = b.worktree, b.branch
    repo = repo or target.checkout
    return (
        f"systemctl --user stop {b.unit} 2>/dev/null; "
        f"if systemctl --user is-active --quiet {b.unit}; then "
        f'echo "kept: {wt} — unit {b.unit} still active after stop"; '
        f"else "
        f"systemctl --user reset-failed {b.unit} 2>/dev/null; "
        f"git -C {repo} worktree unlock {wt} 2>/dev/null; "
        f"if [ -e {wt}/.git ]; then "
        f"git -C {repo} worktree remove --force {wt} >/dev/null 2>&1; "
        f"else rm -rf {wt}; "
        f"git -C {repo} worktree remove --force {wt} >/dev/null 2>&1; "
        f"fi; "
        f'if [ -e {wt} ]; then echo "kept: {wt} — worktree not removed"; '
        f"elif git -C {repo} show-ref --verify --quiet refs/heads/{branch} "
        f"&& ! git -C {repo} branch -D {branch} >/dev/null 2>&1; then "
        f'echo "kept: {wt} — branch {branch} not deleted"; '
        f'else echo "removed: {wt} (abandoned)"; '
        f"fi; "
        f"fi"
    )


def _release(tools: Tools, run: Manifest, b: Batch, target: Target) -> bool:
    """Release the batch's claims, printing the outcome; False when the release failed.

    A server batch was claimed under the orchestrator's branch the manifest records, never
    under HEAD now: this may run from another worktree. Another repo's batch was claimed under
    its own branch in that repo's register.
    """
    holder = run.orchestrator_branch if target.is_server else b.branch
    argv = ["release", *(str(n) for n in b.issues), "--worktree", holder]
    if not target.is_server:
        argv += ["--repo", target.repo]
    argv += ["--reason", f"fan-out batch {b.batch} abandoned"]
    issues = ", ".join(f"#{n}" for n in b.issues)
    # The manifest may already be gone, so a failed release prints its own retry: a second
    # `abandon` stops at "no manifest".
    retry = "findings.py " + shlex.join(argv)
    try:
        proc = tools.findings(argv)
    except subprocess.TimeoutExpired:
        print(f"  release timed out; run: {retry}")
        return False
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout).split())
        print(f"  release failed ({proc.returncode}): {detail}; run: {retry}")
        return False
    print(f"  released {issues} from `{holder}`")
    return True


def cmd_abandon(args, tools: Tools) -> int:
    """Discard one unmerged batch's tree and branch, record it, and release its claims.

    The removal is recorded in the manifest before the release runs: the tree is gone either
    way, and `launch` reads `removed_at` to allow a relaunch of those issues. The manifest is
    deleted once every batch carries one, as `clean` does.

    Returns:
        0 when the tree, the branch and the claims are all gone; 1 otherwise.
    """
    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    index = next((i for i, b in enumerate(run.batches) if b.batch == args.batch), None)
    if index is None:
        names = ", ".join(b.batch for b in run.batches)
        print(
            f"abandon: run {run.run_id} has no batch {args.batch} (it has {names})",
            file=sys.stderr,
        )
        return 1
    b = run.batches[index]
    if b.removed_at:
        print(f"{b.batch} on {b.host}: removed earlier ({b.removed_at})")
        return 0
    try:
        target = resolve(b.repo, tools.default_ref)
    except ValueError as exc:
        print(f"{b.batch} on {b.host}: abandon failed: {exc}")
        return 1
    command = remote_abandon_command(b, target=target)
    try:
        proc = tools.run(b.host, command, 120.0, None)
    except subprocess.TimeoutExpired:
        print(f"{b.batch} on {b.host}: abandon timed out")
        return 1
    verdict, line = read_clean_result(proc)
    print(f"{b.batch} on {b.host}: {line}")
    if verdict != "removed":
        return 1
    run.batches[index] = dataclasses.replace(
        b, removed_at=datetime.now(UTC).isoformat()
    )
    if all(x.removed_at for x in run.batches):
        manifest_mod.path(run.run_id, args.manifest_root).unlink(missing_ok=True)
    else:
        manifest_mod.save(run, root=args.manifest_root)
    return 0 if _release(tools, run, b, target) else 1
