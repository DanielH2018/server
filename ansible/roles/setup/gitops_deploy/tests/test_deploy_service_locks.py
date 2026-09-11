#!/usr/bin/env python3
"""`deploy_locks.service_locks`: which locks a deploy takes, and who they exclude.

ADR-0017 moved the serialization a deploy needs off the tree lock and onto one lock per
service. What has to be true is narrow: a scoped deploy excludes another deploy of the SAME
service and nothing else, and a run naming no service excludes everything.

The locks here are real `fcntl` locks, on the tmp_path directory the `service_lock_dir` fixture
redirects them to. Nothing patches a module: `service_locks` is driven directly, and what ties
the deployer's three playbook call sites to it is
`ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py`.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_service_locks.py
"""

import fcntl
import os
import time

import deploy_locks
import pytest
from _deploy_fakes import locks_taken


def _hold(lock_dir, name: str, mode: int = fcntl.LOCK_EX) -> int:
    """Flock `server-deploy-<name>.lock`, as another deploy would; the open descriptor."""
    path = os.path.join(lock_dir, f"server-deploy-{name}.lock")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    fcntl.flock(fd, mode | fcntl.LOCK_NB)
    return fd


def test_a_scoped_deploy_takes_all_plus_one_lock_per_service(service_lock_dir):
    """CLEAN half: the locks a deploy is supposed to take, and no others.

    Order matters as much as membership — `all` before any service is what keeps a full run
    and a scoped run from deadlocking — so the yielded list is asserted, not a set.
    """
    with deploy_locks.service_locks({"sonarr", "radarr"}, timeout=5) as taken:
        assert taken == ["all", "radarr", "sonarr"]
    assert locks_taken(service_lock_dir) == ["all", "radarr", "sonarr"]


def test_a_run_naming_no_service_takes_only_the_all_lock(service_lock_dir):
    """CLEAN half for the other shape: there is no service to name, so `all` carries it."""
    with deploy_locks.service_locks([], timeout=5) as taken:
        assert taken == ["all"]
    assert locks_taken(service_lock_dir) == ["all"]


def test_a_deploy_waits_for_another_deploy_of_the_same_service(service_lock_dir):
    """FLAGGED half: without the lock this returns at once and two rollouts race.

    The held lock is never released, so the wait can only end at the timeout — and it must end
    by RAISING, because a deploy that could not take its lock must not proceed.
    """
    held = _hold(service_lock_dir, "sonarr")
    try:
        started = time.monotonic()
        entered = False
        with pytest.raises(RuntimeError, match="service lock sonarr"):
            with deploy_locks.service_locks({"sonarr"}, timeout=1):
                entered = True
        assert not entered, "the body ran without holding its lock"
        assert time.monotonic() - started >= 1
    finally:
        os.close(held)


def test_a_deploy_of_a_different_service_is_not_blocked(service_lock_dir):
    """FLAGGED half for over-locking: one global lock would pass the test above and fail here.

    That is the whole change — a deploy of radarr must not wait behind a deploy of sonarr.
    """
    held = _hold(service_lock_dir, "sonarr")
    try:
        with deploy_locks.service_locks({"radarr"}, timeout=1) as taken:
            assert taken == ["all", "radarr"]
    finally:
        os.close(held)


def test_a_run_naming_no_service_is_blocked_by_a_scoped_run(service_lock_dir):
    """FLAGGED half for the `all` lock's shared/exclusive split.

    A scoped deploy holds `all` shared. A run naming no service wants it exclusive, so it
    queues — which is what stops a whole-playbook apply landing on a running service deploy.
    """
    held = _hold(service_lock_dir, "all", fcntl.LOCK_SH)
    try:
        with pytest.raises(RuntimeError, match="service lock all"):
            with deploy_locks.service_locks([], timeout=1):
                pass
    finally:
        os.close(held)


def test_two_scoped_runs_share_the_all_lock(service_lock_dir):
    """CLEAN half for the same split: shared must not exclude shared.

    Without it `all` would be one global lock again and nothing would ever overlap.
    """
    held = _hold(service_lock_dir, "all", fcntl.LOCK_SH)
    try:
        with deploy_locks.service_locks({"radarr"}, timeout=1) as taken:
            assert taken == ["all", "radarr"]
    finally:
        os.close(held)


def test_the_locks_are_released_when_the_body_raises(service_lock_dir):
    """FLAGGED half for the `finally`: a deploy that fails must not hold its lock forever.

    A leaked descriptor is invisible until the NEXT deploy of that service queues for the full
    timeout behind a process that is no longer deploying anything.
    """
    with pytest.raises(ValueError):
        with deploy_locks.service_locks({"sonarr"}, timeout=1):
            raise ValueError("the playbook failed")
    # Takeable again means released: this would raise if the first hold had leaked.
    with deploy_locks.service_locks({"sonarr"}, timeout=1) as taken:
        assert taken == ["all", "sonarr"]
