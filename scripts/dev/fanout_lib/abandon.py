"""Stop or abandon fan-out batches: `fanout_place.py stop` and `fanout_place.py abandon`.

`clean` keeps an unmerged tree by design, so giving up on a batch took a hand-run teardown on
its host: unlock, force-remove, `branch -D`, a second `clean`, then one `findings.py release`
per issue (#3919, #3924). `abandon` is that teardown as one command. It discards whatever the
agent committed and stops the batch's agent, which is why it names exactly one batch.

`stop` ends the agents and keeps their trees for `clean`. Both release the batches' claims,
which sit under the orchestrator's branch and so outlive any `reap` (#3925).
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
from fanout_lib import status as status_mod
from fanout_lib.clean import busy_refusal, live_process_scan, read_clean_result
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
        f"{live_process_scan(wt)}"
        f'if [ -n "$busy" ]; then {busy_refusal(wt)}; else '
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
        f"fi; "
        f"fi"
    )


def release_batch(
    tools: Tools, run: Manifest, b: Batch, target: Target, why: str
) -> bool:
    """Release the batch's claims, printing the outcome; False when the release failed.

    A server batch was claimed under the orchestrator's branch the manifest records, never
    under HEAD now: this may run from another worktree. Another repo's batch was claimed under
    its own branch in that repo's register.
    """
    holder = run.orchestrator_branch if target.is_server else b.branch
    argv = ["release", *(str(n) for n in b.issues), "--worktree", holder]
    if not target.is_server:
        argv += ["--repo", target.repo]
    argv += ["--reason", f"fan-out batch {b.batch} {why}"]
    issues = ", ".join(f"#{n}" for n in b.issues)
    # The manifest may already be gone, so a failed release prints its own retry: a second
    # `abandon` stops at "no manifest".
    retry = "findings.py " + shlex.join(argv)
    try:
        proc = tools.findings(argv)
    except subprocess.TimeoutExpired:
        print(f"  release timed out; run: {retry}")
        return False
    refusals = [ln for ln in proc.stdout.splitlines() if " refused: " in ln]
    # `stop` then `abandon` releases twice. An issue nobody holds any more is the goal state,
    # so only a refusal naming another holder, or a failure with no refusal line, fails.
    if (
        proc.returncode == 3
        and refusals
        and all(ln.endswith("not claimed") for ln in refusals)
    ):
        print(f"  claims on {issues} already released")
        return True
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
    return 0 if release_batch(tools, run, b, target, "abandoned") else 1


def cmd_stop(args, tools: Tools) -> int:
    """Stop each batch's unit, or the one named, and release the claims it held.

    Returns:
        1 when a release failed, else 0.
    """
    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    failed = False
    for b in run.batches:
        if args.batch and b.batch != args.batch:
            continue
        # `clean` already reset this batch's unit and took its worktree. Stopping it again
        # reads systemd's "Unit … not loaded" as the batch's status and then tells the
        # operator to clean a tree that is gone.
        if b.removed_at:
            print(f"{b.batch} on {b.host}: cleaned ({b.removed_at})")
            continue
        try:
            proc = tools.run(b.host, status_mod.stop_command(b.unit), 30.0, None)
        except subprocess.TimeoutExpired:
            print(f"{b.batch} on {b.host}: stop timed out")
            continue
        print(
            f"{b.batch} on {b.host}: {'stopped' if proc.returncode == 0 else proc.stderr.strip()}"
        )
        # A stopped agent works nothing, so its claims go back (#3925). An open PR it left
        # still withholds the issue from `next`, so nobody duplicates work that may merge.
        if proc.returncode == 0:
            try:
                target = resolve(b.repo, tools.default_ref)
            except ValueError as exc:
                print(f"  claims not released: {exc}")
                failed = True
            else:
                failed = not release_batch(tools, run, b, target, "stopped") or failed
        # The worktree stays locked until `clean` runs it through lib.worktrees' content
        # check — stopping a batch says nothing about whether its PR merged.
        print(f"  run `clean {args.run_id}` once its PR merges")
        # A landing or detached deploy left the unit's cgroup for its own scope (#3160), so
        # the stop above did not end it.
        print(
            "  a landing or detached deploy it started keeps running in its own scope; "
            f"on {b.host}: systemctl --user list-units 'land*' 'deploy-*'"
        )
    return 1 if failed else 0
