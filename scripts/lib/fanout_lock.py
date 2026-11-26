"""Which worktree locks are a fan-out batch's, and whether another user's batch still runs.

`fanout_lib.launch` locks each batch worktree with the batch's systemd user unit name as the
reason (`fanout-<batch>`, or `fanout-<repo>-<batch>` for another repo). That reason carries
no pid, so `session_is_alive` cannot judge it and reads it as live. Read that way, a tree
`fanout.py place clean` never removed survives every weekly sweep, and such trees accumulate
on the host's root filesystem.

The rule that judges such a lock is `claude_worktree.unit_lock_is_live`: the unit's state
decides, and a lock younger than a day is live whatever the unit says. This module holds the
two things that rule cannot know. `REASON` says which lock reasons name a unit, and
`.claude/prune-worktrees.json` hands the same pattern to the worktree pruner as `unit_locks`
(`scripts/lib/tests/test_fanout_lock.py::test_the_pruner_policy_carries_the_fanout_lock_pattern`).
`process_unit_state` judges a tree another user owns, for a caller that only reports, never
removes. It reads the unit names in `/proc/<pid>/cgroup`, which every user can read, where
that user's manager answers only its owner.
"""

import functools
import os
import re
from collections.abc import Callable
from pathlib import Path

REASON = re.compile(r"fanout(?:-[A-Za-z][\w.]*)*(?:-\d+)+")


def running_units(proc: str = "/proc") -> frozenset[str] | None:
    """The systemd units, under any user's manager, that hold at least one process.

    Returns:
        Unit names without the `.service` suffix, or None when `proc` cannot be listed.
    """
    try:
        pids = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return None
    units = set()
    for pid in pids:
        try:
            text = Path(proc, pid, "cgroup").read_text()
        except OSError:
            continue
        for line in text.splitlines():
            units.update(
                part.removesuffix(".service")
                for part in line.split("/")
                if part.endswith(".service")
            )
    return frozenset(units)


def process_unit_state(proc: str = "/proc") -> Callable[[str], str | None]:
    """A `claude_worktree.unit_state`-shaped reader for any user's units, from one scan of `proc`.

    The scan runs on the first call, and every later call reuses it. A unit with no process
    reads `inactive`; an unreadable `proc` reads None.
    """
    units = functools.cache(lambda: running_units(proc))

    def state(unit: str) -> str | None:
        found = units()
        if found is None:
            return None
        return "active" if unit in found else "inactive"

    return state


def is_fanout_reason(reason: str) -> bool:
    return REASON.fullmatch(reason) is not None
