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

    with locked_budget({"sonarr", "radarr"}, 900) as budget:
        run(argv, cwd=repo, timeout=budget)
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
# The wait a caller with no budget of its own gets. Only another deploy of the same service can
# hold one of these locks, and a full `ansible/deploy.yml` measured 1212s on 2026-08-22 -- the
# same ceiling `gitops_deploy_broad_timeout_s` gives one apply. Waiting forever is not an option:
# this process holds the git-tree lock while it waits, so an unbounded wait parks every other
# job on that lock behind a deploy that is doing nothing.
SERVICE_LOCK_WAIT_S = 1800.0
# What `locked_budget` yields when the wait consumed the whole budget. A zero or negative
# `subprocess.run(timeout=)` kills the child immediately, which reads as a deploy failure rather
# than as contention; one second fails the same way but leaves the argv in the log.
MIN_RUN_BUDGET_S = 1.0


class ServiceLockBusy(RuntimeError):
    """Another deploy of one of these services held its lock for the whole budget.

    Contention, not failure: nothing was applied, so the caller undoes its range and lets the
    next tick re-evaluate rather than holding the SHA and rolling back. The message is the
    journal line's subject — `service lock <tag> busy for <N>s`.
    """


def lock_dir() -> str:
    """Where the locks live. `HOMELAB_DEPLOY_LOCK_DIR` redirects them, as it does for deploy.sh.

    Read per call rather than at import so a test can redirect it without patching a module
    attribute. The deployed unit never sets the variable.
    """
    return os.environ.get("HOMELAB_DEPLOY_LOCK_DIR", "/var/lock")


def _take(name: str, mode: int, deadline: float) -> tuple[str, int]:
    """Flock one service lock and return its name and open descriptor.

    Args:
        name: the service tag, or SERVICE_LOCK_ALL.
        mode: fcntl.LOCK_EX or fcntl.LOCK_SH.
        deadline: the `time.monotonic()` value to give up at.

    Raises:
        ServiceLockBusy: the lock stayed busy past `deadline`.
        OSError: the lock file could not be opened.
    """
    path = os.path.join(lock_dir(), f"server-deploy-{name}.lock")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return name, fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                waited = round(time.monotonic() - started)
                raise ServiceLockBusy(
                    f"service lock {name} busy for {waited}s"
                ) from None
            time.sleep(SERVICE_LOCK_POLL_S)


@contextlib.contextmanager
def service_locks(
    services: Iterable[str],
    timeout: float = SERVICE_LOCK_WAIT_S,
    exclusive_all: bool = False,
):
    """Hold one lock per service for the body, in the order the DECIDED note above fixes.

    Args:
        services: the tags this deploy names. Empty means the whole playbook.
        timeout: seconds to wait for the locks. A caller whose phase carries a declared budget
            calls `locked_budget` instead, so that the wait and the run SHARE that budget
            rather than each being given one.
        exclusive_all: take `all` exclusively even when `services` names tags. A scoped SERVICE
            deploy shares it, because two of those do not conflict. A broad-plane apply is not
            a service deploy — `initial_setup.yml --tags <role>` reconfigures the host every
            workload runs on — so it excludes every other deploy whatever its tags say.

    Yields:
        The lock names taken, in the order they were taken.

    Raises:
        ServiceLockBusy: a lock stayed busy past `timeout`. Nothing was deployed.
    """
    names = sorted(set(services))
    deadline = time.monotonic() + timeout
    held: list[tuple[str, int]] = []
    try:
        held.append(
            _take(
                SERVICE_LOCK_ALL,
                fcntl.LOCK_SH if names and not exclusive_all else fcntl.LOCK_EX,
                deadline,
            )
        )
        for name in names:
            held.append(_take(name, fcntl.LOCK_EX, deadline))
        yield [name for name, _ in held]
    finally:
        for _, fd in held:
            os.close(fd)


@contextlib.contextmanager
def locked_budget(services: Iterable[str], timeout: float, exclusive_all: bool = False):
    """Take the service locks and yield what is LEFT of `timeout` for the caller's run.

    One deadline covers the wait and the work, which is what keeps a phase's declared timeout a
    true bound on how long this process holds the git-tree lock. A wait with a budget of its own
    would double every k8s term in `_worst_lock_hold()`
    (`tests/test_gitops_deploy_timeout_budgets.py`), and the four jobs that wait on the tree lock
    derive their own waits from that sum — so a deploy queued behind an operator's would make
    each of them give up and page for ordinary contention.

    Args:
        services: the tags this deploy names. Empty means the whole playbook.
        timeout: the phase's whole budget, in seconds.
        exclusive_all: as `service_locks`.

    Yields:
        The seconds left for the run, never below `MIN_RUN_BUDGET_S`.

    Raises:
        ServiceLockBusy: a lock stayed busy past `timeout`. Nothing was deployed.
    """
    deadline = time.monotonic() + timeout
    with service_locks(services, timeout, exclusive_all):
        yield max(MIN_RUN_BUDGET_S, deadline - time.monotonic())
