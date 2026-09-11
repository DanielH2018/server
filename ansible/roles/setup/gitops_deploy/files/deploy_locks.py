# ansible/roles/setup/gitops_deploy/files/deploy_locks.py
"""The per-service locks a deploy holds across its playbook.

`/var/lock/server-git-tree.lock` guards the git tree and nothing else (ADR-0011). What guards
the CLUSTER is one lock per deploy tag, `server-deploy-<tag>.lock`, plus `server-deploy-all.lock`
for a run that names no tag (ADR-0017). `scripts/deploy.sh` takes the same locks by the same
names, which is what makes an operator deploy and this unit exclude each other on a service
rather than on the whole tree.

A leaf: it imports nothing from the rest of the deployer, so a test can drive it directly.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.

Typical usage example:

    with service_locks({"sonarr", "radarr"}, timeout=900):
        run(argv, cwd=repo, timeout=900)
"""

import contextlib
import fcntl
import os
import time
from collections.abc import Iterable

# DECIDED: the lock order is `server-deploy-all.lock` first -- shared when the run names
# services, exclusive when it names none -- then each service's own lock in sorted order.
# Sorted order is what makes two overlapping scoped runs deadlock-free; taking `all` before any
# service is what makes a full run and a scoped run deadlock-free. The deployer takes the TREE
# lock (systemd's ExecStart `flock`) and then these, holding both across the playbook.
# `scripts/deploy.sh` takes the tree lock, snapshots, RELEASES it, and only then takes these --
# and never re-takes the tree lock -- so the two orders cannot form a cycle. (ADR-0017)

# The lock a run with no tags takes exclusively, and every scoped run takes shared.
SERVICE_LOCK_ALL = "all"
# How often a blocked acquire retries. `fcntl.flock` has no timeout of its own, and
# `signal.alarm` would interrupt whatever else this process happens to be in the middle of.
SERVICE_LOCK_POLL_S = 0.5


def lock_dir() -> str:
    """Where the locks live. `HOMELAB_DEPLOY_LOCK_DIR` redirects them, as it does for deploy.sh.

    Read per call rather than at import so a test can redirect it without patching a module
    attribute. The deployed unit never sets the variable.
    """
    return os.environ.get("HOMELAB_DEPLOY_LOCK_DIR", "/var/lock")


def _take(name: str, mode: int, deadline: float | None) -> tuple[str, int]:
    """Flock one service lock and return its name and open descriptor.

    Args:
        name: the service tag, or SERVICE_LOCK_ALL.
        mode: fcntl.LOCK_EX or fcntl.LOCK_SH.
        deadline: a `time.monotonic()` value to give up at, or None to wait indefinitely.

    Raises:
        RuntimeError: the lock stayed busy past `deadline`.
        OSError: the lock file could not be opened.
    """
    path = os.path.join(lock_dir(), f"server-deploy-{name}.lock")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return name, fd
        except OSError:
            if deadline is not None and time.monotonic() >= deadline:
                os.close(fd)
                raise RuntimeError(
                    f"service lock {name} ({path}) stayed busy; nothing was deployed"
                ) from None
            time.sleep(SERVICE_LOCK_POLL_S)


@contextlib.contextmanager
def service_locks(services: Iterable[str], timeout: float | None = None):
    """Hold one lock per service for the body, in the order the DECIDED note above fixes.

    Args:
        services: the tags this deploy names. Empty means the whole playbook.
        timeout: seconds to wait for the locks, or None to wait indefinitely — the same budget
            the caller already bounds its own `run` with. The Docker deploy passes None because
            its `run` is unbounded too, so the wait and the work share one budget.

    Yields:
        The lock names taken, in the order they were taken.

    Raises:
        RuntimeError: a lock stayed busy past `timeout`. Nothing was deployed.
    """
    names = sorted(set(services))
    deadline = None if timeout is None else time.monotonic() + timeout
    held: list[tuple[str, int]] = []
    try:
        held.append(
            _take(
                SERVICE_LOCK_ALL,
                fcntl.LOCK_SH if names else fcntl.LOCK_EX,
                deadline,
            )
        )
        for name in names:
            held.append(_take(name, fcntl.LOCK_EX, deadline))
        yield [name for name, _ in held]
    finally:
        for _, fd in held:
            os.close(fd)
