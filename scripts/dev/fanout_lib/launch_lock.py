"""The lock that lets one `fanout.py place launch` run at a time for a user on a host.

`launch` reads every run manifest at its gates and saves its own only as each batch starts.
Two launches that overlap both read the manifests before either has written, so both pass
the live-batch cap and the file gates. Refill waits make the overlap the normal case: every
waiting orchestrator wakes on the same freed slot.

The lock is an exclusive `flock` on one file under the manifest root, so it covers the
launches that share that root: one user on one host. The kernel releases it when the holder
exits, however it exits, so a killed launch leaves nothing to clear. The file holds the
holder's pid and run id, which a waiter prints when its wait ends without the lock.

    with launch_lock.held(root, name_run) as run_id:   # `launch`
        ...
    launch_lock.holder(root)                            # the `fanout-headroom` wait source
"""

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

# Not `*.json`: `manifest.all_runs` globs the root for run manifests.
LOCK_NAME = "launch.lock"
WAIT_S = 120.0
POLL_S = 0.2


@dataclass(frozen=True)
class Holder:
    """The launch holding the lock, as its record names it.

    Attributes:
        pid: the holder's process id, or 0 when the record could not be read.
        run: the run id the holder launches under, or "" when the record could not be read.
    """

    pid: int = 0
    run: str = ""

    def __str__(self) -> str:
        if not self.pid:
            return "another launch (its record could not be read)"
        return f"pid {self.pid} (run {self.run})"


class LockTimeout(Exception):
    """The wait for the lock ended with another launch still holding it.

    Attributes:
        holder: the launch that held the lock when the wait ended.
    """

    def __init__(self, holder: Holder):
        super().__init__(f"launch lock held by {holder}")
        self.holder = holder


def path(root: Path) -> Path:
    """The lock file for the launches that keep their manifests under `root`."""
    return root / LOCK_NAME


def _record(fd: int) -> Holder:
    """The holder record in the open lock file; an empty `Holder` when it does not parse."""
    try:
        data = json.loads(os.pread(fd, 4096, 0))
        return Holder(int(data["pid"]), str(data["run"]))
    except ValueError, KeyError, TypeError:
        return Holder()


def _try(fd: int, mode: int) -> bool:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def held(
    root: Path, name_run: Callable[[], str], wait_s: float = WAIT_S
) -> Generator[str]:
    """Hold the launch lock for the body, yielding the run id the holder record names.

    Args:
        root: the manifest directory, created if absent.
        name_run: picks the run id, called once the lock is held, so it reads the
            manifests no other launch can still be adding to.
        wait_s: how long to wait for another launch to release the lock.

    Raises:
        LockTimeout: another launch still held the lock after `wait_s`.
    """
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(path(root), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while not _try(fd, fcntl.LOCK_EX):
            if time.monotonic() >= deadline:
                raise LockTimeout(_record(fd))
            time.sleep(POLL_S)
        run_id = name_run()
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"pid": os.getpid(), "run": run_id}).encode(), 0)
        yield run_id
    finally:
        os.close(fd)


def holder(root: Path) -> Holder | None:
    """The launch holding the lock under `root`, or None when no launch holds it.

    Takes and drops a shared lock on a read-only descriptor, so it never truncates the
    holder record and two readers never block each other.
    """
    try:
        fd = os.open(path(root), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        return None if _try(fd, fcntl.LOCK_SH) else _record(fd)
    finally:
        os.close(fd)
