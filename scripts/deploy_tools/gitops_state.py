#!/usr/bin/env python3
"""Operate on the GitOps deployer's own state markers, from the deploy host's shell.

One subcommand today. `clear-manual-plane <role>` drops a role's line from
`/var/lib/gitops-deploy/manual_plane`, the marker the deployer writes when a range carries a
setup role no playbook it runs can apply — `k3s` (applied by `k3s-bringup.yml`) or `common`
(applied by no playbook at all). The tick fast-forwards past such a range rather than parking
it, so the marker is what says the apply is still owed: monitor-bridge pages once the oldest
pending role is six hours old, and `land.sh` prints the same clear command.

**The apply comes first, this second.** Clearing a role nobody applied silences the only
durable signal that it is unapplied, which is the state the marker exists to make visible.

This is not a path the deployer takes. Its own reverse is
`DeployerState.clear_manual_plane_applied`, which fires when a tick applies the role's real
playbook and tag — unreachable today, since it runs neither of the two playbooks in question.

WHERE IT RUNS. `/var/lib/gitops-deploy` is 0750 and owned by `sys_user` (`ubuntu` on
daniel-box), so the deploy user's own shell writes it directly and any other user needs
`sudo -u ubuntu`. A directory this uid cannot write is reported as that, not as a traceback.

The rewrite takes `/var/lock/server-git-tree.lock`, the lock a tick already holds, so the two
cannot interleave over the same file. It waits seconds rather than minutes and then refuses:
re-run it once the deploy or tick finishes.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import argparse
import contextlib
import fcntl
import os
import sys
import time

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES

# The deployer's own modules, so this reads and rewrites the marker through the code that
# writes it rather than through a second copy of the format. `deploy_state` reaches
# `host_lib` for its atomic write, which is why both directories go on the path — unlike the
# tools that import `deploy_logic`, which must stay on `files/` alone.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
_sys.path.insert(0, str(HOST_LIB_FILES))

from deploy_changes import setup_role_tag
from deploy_state import STATE_DIR, DeployerState

# The tree lock every writer of this host's checkout takes: `deploy.sh`'s own `LOCK=`, and the
# deployer unit's `flock` ExecStart.
TREE_LOCK = "/var/lock/server-git-tree.lock"

# Seconds to wait for it. Every other waiter on this lock waits 3000 (the census in the
# deployer's test_gitops_deploy_timeout_budgets.py), because those are unattended jobs that
# must not skip their run. This is an operator at a prompt, and a tick can hold the lock for
# most of an hour, so waiting it out would read as a hang. Refusing is the better answer: the
# clear changes one line, is idempotent, and costs nothing to re-run.
LOCK_WAIT_S = 5.0


class LockBusy(Exception):
    """The tree lock stayed held for the whole wait, so nothing was read or written."""


class LockUnavailable(Exception):
    """The lock FILE could not be opened at all — a wrong mode, or a missing directory.

    Separate from `LockBusy`, and separate from the state directory's own `PermissionError`:
    all three exit 1, and an operator needs to know which of the two paths they cannot reach.

    Attributes:
        args: the lock path, then the `OSError` that explains it.
    """


@contextlib.contextmanager
def tree_lock(path: str, wait_s: float | None = None):
    """Hold the tree lock across a read-modify-write of the marker, or raise `LockBusy`.

    `DeployerState.record_manual_plane` reads every line, appends one and writes the file
    back; so does `clear_manual_plane`. Interleaved, the loser's write drops the winner's
    line — a role recorded and then silently lost, or a cleared role reappearing. The tick
    already runs under this lock, so taking it here is what makes the pair safe.

    Args:
      wait_s: how long to wait for the lock. None reads `LOCK_WAIT_S` at call time, which is
        what lets a test shorten the wait rather than sleep through it.

    Raises:
      LockBusy: the lock was held for the whole wait.
      LockUnavailable: the lock file could not be opened. Raised rather than left as a bare
        OSError so the caller cannot attribute it to the state directory, which has its own
        PermissionError and its own remediation.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    except OSError as exc:
        raise LockUnavailable(path, exc) from exc
    try:
        deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LockBusy(path) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def marker_key(role: str) -> str:
    """The `manual_plane` line key for a role, which is the `--tags` value that selects it."""
    return setup_role_tag(role)


def clear_manual_plane(
    state: DeployerState,
    role: str,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
) -> int:
    """Drop `role`'s pending line. Exit 0 whether or not there was one to drop.

    Args:
      lock_path: the tree lock to serialise the rewrite against. None reads `TREE_LOCK`.
      lock_wait_s: how long to wait for it. None reads `LOCK_WAIT_S`.
    """
    key = marker_key(role)
    try:
        with tree_lock(TREE_LOCK if lock_path is None else lock_path, lock_wait_s):
            cleared = state.clear_manual_plane(key)
    except LockBusy as busy:
        print(
            f"{busy.args[0]} is held — a deploy or a gitops tick is running. Nothing was "
            "changed; re-run this when it finishes.",
            file=sys.stderr,
        )
        return 1
    except LockUnavailable as bad_lock:
        path, exc = bad_lock.args
        print(
            f"cannot open the tree lock {path}: {exc}. Nothing was changed — this command "
            "serialises against that lock and will not write the marker without it.",
            file=sys.stderr,
        )
        return 1
    except PermissionError:
        print(
            f"cannot write {state.path('manual_plane')} as this user — the state directory "
            "is owned by the deploy user; retry with `sudo -u ubuntu`",
            file=sys.stderr,
        )
        return 1
    if not cleared:
        print(
            f"{role} is not pending in {state.path('manual_plane')} — nothing to clear"
        )
        return 0
    print(f"cleared {role} from {state.path('manual_plane')}")
    return 0


def main(
    argv: list[str] | None = None,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
) -> int:
    """Parse `argv` and run the subcommand it names.

    Args:
      lock_path: the tree lock the rewrite serialises against. None reads `TREE_LOCK`; a test
        passes its own, because taking the host's real lock would block a running deploy.
      lock_wait_s: how long to wait for it. None reads `LOCK_WAIT_S`.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-dir",
        default=STATE_DIR,
        help=f"the deployer's state directory (default: {STATE_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    clear = sub.add_parser(
        "clear-manual-plane",
        help="drop one setup role's pending line, AFTER applying it by hand",
    )
    clear.add_argument("role", help="the setup role, e.g. k3s or common")
    args = parser.parse_args(argv)
    state = DeployerState(args.state_dir)
    if args.command != "clear-manual-plane":
        # argparse refuses any other value, so this catches a subcommand added to the parser
        # and not to this dispatch — which would otherwise run the clear with its arguments.
        parser.error(f"no handler for {args.command}")
    return clear_manual_plane(state, args.role, lock_path, lock_wait_s)


if __name__ == "__main__":
    sys.exit(main())
