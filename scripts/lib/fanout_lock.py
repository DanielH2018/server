"""Whether a fan-out batch's worktree lock still protects a running batch.

`fanout_lib.launch` locks each batch worktree with the batch's systemd user unit name as the
reason (`fanout-<batch>`, or `fanout-<repo>-<batch>` for another repo). That reason carries
no pid, so `session_is_alive` cannot judge it and reads it as live. Read that way, a tree
`fanout.py place clean` never removed survived every weekly sweep: daniel-server held 152 of
them, 24 GiB of its root filesystem, on 2026-11-14 (#8529).

The lock's owner is the unit, so the unit's state decides. Every doubt reads as live: a
manager that cannot be asked, a tree another user owns (its units sit under that user's
manager, not the caller's), and a lock younger than `GRACE_S`, because launch takes the lock
before `systemd-run` starts the unit and a fresh tree at master already reads as merged.
"""

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# `fanout_lib.target.Target.unit_prefix` plus `fanout_lib.launch.unit_name`'s batch id: issue
# numbers joined by hyphens. A repo-name segment starts with a letter, so it never reads as one.
REASON = re.compile(r"fanout(?:-[A-Za-z][\w.]*)*(?:-\d+)+")
GRACE_S = 86400
DEAD_STATES = frozenset({"inactive", "failed"})


def unit_state(unit: str) -> str | None:
    """The caller's systemd user manager's ActiveState for `unit`.service.

    A unit the manager does not know reads `inactive`, which is what a finished batch's
    transient unit becomes once it is collected.

    Returns:
        The state string, or None when the manager could not be asked. A system service
        running as a user has no `XDG_RUNTIME_DIR`, so this supplies the caller's own.
    """
    env = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", **os.environ}
    command = ["systemctl", "--user", "show", "-p", "ActiveState", "--value"]
    try:
        proc = subprocess.run(
            [*command, f"{unit}.service"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def lock_age(tree: str, now: float) -> float | None:
    """Seconds from `git worktree lock` writing `tree`'s lock file to `now`, or None."""
    try:
        gitdir = Path(tree, ".git").read_text().removeprefix("gitdir:").strip()
        return now - Path(gitdir, "locked").stat().st_mtime
    except OSError:
        return None


def is_fanout_reason(reason: str) -> bool:
    return REASON.fullmatch(reason) is not None


def batch_is_live(
    tree: str,
    reason: str,
    state: Callable[[str], str | None] = unit_state,
    now: float | None = None,
) -> bool:
    """Whether the batch unit named by lock `reason` may still be using `tree`.

    Args:
        tree: the worktree's path.
        reason: its lock reason, which `is_fanout_reason` has accepted.
        state: `unit_state`-shaped; a test seam.
        now: the time to measure the lock's age at; the clock's when None.
    """
    try:
        if Path(tree).stat().st_uid != os.getuid():
            return True
    except OSError:
        return True
    age = lock_age(tree, time.time() if now is None else now)
    if age is None or age < GRACE_S:
        return True
    return state(reason) not in DEAD_STATES
