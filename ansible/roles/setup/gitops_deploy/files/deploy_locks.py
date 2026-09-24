# ansible/roles/setup/gitops_deploy/files/deploy_locks.py
"""The per-service locks a deploy holds across its playbook.

`/var/lock/server-git-tree.lock` guards the git tree and nothing else (ADR-0011). What guards
the CLUSTER is one lock per deploy tag, `server-deploy-<tag>.lock`, plus `server-deploy-all.lock`
for a run that names no tag (ADR-0017). `scripts/deploy.sh` takes the same locks by the same
names, which is what makes an operator deploy and this unit exclude each other on a service
rather than on the whole tree.

THIS MODULE IS THE ONLY PLACE THAT NAMES AND ORDERS THEM. The wrapper does not build a lock
name or sort a tag list of its own: it calls `plan` in process
(`scripts/deploy_tools/deploy_under_locks.py`) and takes the locks it is handed, in that
order. Until 2026-09-18 the shell carried its own copy of both, and
the two agreed only because a test compared them -- `sort` and Python's `sorted` disagree on
`pihole` against `pi-peer-backup` unless the shell pins `LC_ALL=C`, and a disagreement there
is a deadlock between a hand deploy and a tick (issue #2054).

A leaf: it imports nothing from the rest of the deployer, so a test can drive it directly.

Stdlib only: the unit runs it under `uv run --no-project`, and `deploy_under_locks.py`
imports it. The CLI below is for reading a plan by hand.

Typical usage example:

    with locked_budget({"sonarr", "radarr"}, 900) as budget:
        run(argv, cwd=repo, timeout=budget)

    $ deploy_locks.py plan sonarr radarr
    all    shared    /var/lock/server-deploy-all.lock
    radarr    exclusive    /var/lock/server-deploy-radarr.lock
    sonarr    exclusive    /var/lock/server-deploy-sonarr.lock
"""

import contextlib
import fcntl
import os
import re
import sys
import time
from collections.abc import Iterable
from typing import NamedTuple

# DECIDED: the lock order is `server-deploy-all.lock` first -- shared when the run names
# services, exclusive when it names none -- then each service's own lock in sorted order.
# Sorted order is what makes two overlapping scoped runs deadlock-free; taking `all` before any
# service is what makes a full run and a scoped run deadlock-free. The deployer takes the TREE
# lock (systemd's ExecStart `flock`) and then these, holding both across the playbook.
# `scripts/deploy.sh` takes the tree lock, snapshots, RELEASES it, and only then takes these --
# and never re-takes the tree lock -- so the two orders cannot form a cycle. (ADR-0017)

# The tree lock (ADR-0011): what the deployer unit's `flock` ExecStart, `deploy.sh` and the
# `gitops_state.py` rewrite all take. Named here so the Python readers share one literal;
# `deploy_under_locks.py` imports it, and the deploy UI, which cannot, is pinned to it by
# `test_lock_names_agree_with_deploy_locks_is_clean`.
TREE_LOCK = "/var/lock/server-git-tree.lock"
# The lock a run with no tags takes exclusively, and every scoped run takes shared.
SERVICE_LOCK_ALL = "all"
# What a lock file may be called. A character outside this set becomes `_`, the substitution
# `deploy.sh` made on its side before the naming moved here; a deploy tag is a containers_list
# key and never needs it, but a name that reached the filesystem unsanitised could carry a `/`.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
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

    Attributes:
        lock: the lock name that stayed busy (a service tag, or SERVICE_LOCK_ALL), for the
            `contention_since` marker. Empty when raised with a message alone.
    """

    def __init__(self, message: str, lock: str = "") -> None:
        super().__init__(message)
        self.lock = lock


def lock_dir() -> str:
    """Where the locks live. `HOMELAB_DEPLOY_LOCK_DIR` redirects them, as it does for deploy.sh.

    Read per call rather than at import so a test can redirect it without patching a module
    attribute. The deployed unit never sets the variable.
    """
    return os.environ.get("HOMELAB_DEPLOY_LOCK_DIR", "/var/lock")


def lock_path(name: str) -> str:
    """The lock file for one service tag, or for SERVICE_LOCK_ALL. The one naming site."""
    return os.path.join(
        lock_dir(), f"server-deploy-{_UNSAFE_NAME_CHARS.sub('_', name)}.lock"
    )


class PlannedLock(NamedTuple):
    """One lock a deploy takes: its name, where it lives, and whether it is taken exclusively."""

    name: str
    path: str
    exclusive: bool


def plan(services: Iterable[str], exclusive_all: bool = False) -> list[PlannedLock]:
    """Every lock a deploy of `services` takes, in the order it takes them.

    The DECIDED note above is the whole rule: `all` first, then each tag once, in code-point
    order. `service_locks` walks this list rather than restating it, and `deploy.sh` takes it
    as returned, so there is one ordering for a deploy to disagree with.

    Args:
        services: the tags the deploy names. Empty means the whole playbook, which takes
            `all` exclusively whatever `exclusive_all` says.
        exclusive_all: take `all` exclusively even with tags -- a broad-plane apply, or the
            wrapper's full run, whose tag list is every declared service.
    """
    names = sorted(set(services))
    shared_all = bool(names) and not exclusive_all
    planned = [
        PlannedLock(SERVICE_LOCK_ALL, lock_path(SERVICE_LOCK_ALL), not shared_all)
    ]
    planned.extend(PlannedLock(name, lock_path(name), True) for name in names)
    return planned


def _take(name: str, path: str, mode: int, deadline: float) -> tuple[str, int]:
    """Flock one service lock and return its name and open descriptor.

    Args:
        name: the service tag, or SERVICE_LOCK_ALL.
        path: the lock file, as `plan` named it.
        mode: fcntl.LOCK_EX or fcntl.LOCK_SH.
        deadline: the `time.monotonic()` value to give up at.

    Raises:
        ServiceLockBusy: the lock stayed busy past `deadline`.
        OSError: the lock file could not be opened.
    """
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
                    f"service lock {name} busy for {waited}s", lock=name
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
    deadline = time.monotonic() + timeout
    held: list[tuple[str, int]] = []
    try:
        for planned in plan(services, exclusive_all):
            mode = fcntl.LOCK_EX if planned.exclusive else fcntl.LOCK_SH
            held.append(_take(planned.name, planned.path, mode, deadline))
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


# -- the CLI, for reading a plan by hand ---------------------------------------------------

USAGE = """usage: deploy_locks.py plan [--exclusive-all] TAG [TAG ...]

Print every lock a deploy of the named tags takes, one per line, in the order to take them:
NAME<TAB>shared|exclusive<TAB>PATH. `--exclusive-all` is the wrapper's full run, whose tag
list is every declared service and which must exclude every scoped run through `all`.
"""


def main(argv: list[str]) -> int:
    """`plan` for the shell. Exit 2 on a usage error, printing nothing a caller could act on.

    A run with no tags is refused rather than planned as a scoped run over nothing: the
    wrapper enumerates a full run's tags itself and hands them over, so an empty argv here is
    a wrapper bug, and a plan of `all` alone would let it deploy everything under one lock.
    """
    if not argv or argv[0] != "plan":
        sys.stderr.write(USAGE)
        return 2
    exclusive_all = False
    tags = []
    for arg in argv[1:]:
        if arg == "--exclusive-all":
            exclusive_all = True
        elif arg.startswith("-"):
            sys.stderr.write(f"deploy_locks.py: unknown option {arg}\n{USAGE}")
            return 2
        elif arg:
            tags.append(arg)
    if not tags:
        sys.stderr.write(f"deploy_locks.py: plan needs at least one tag\n{USAGE}")
        return 2
    for planned in plan(tags, exclusive_all):
        mode = "exclusive" if planned.exclusive else "shared"
        sys.stdout.write(f"{planned.name}\t{mode}\t{planned.path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
