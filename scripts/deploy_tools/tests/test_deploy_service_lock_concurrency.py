#!/usr/bin/env python3
"""Two `deploy.sh` runs overlap when they name different services, and not when they don't.

This is the property ADR-0017 exists for, and the only one that cannot be read off the script:
it is about two processes, so a single-process test would pass whatever the locks did. The tree
lock is still taken for real here -- for the snapshot, which is milliseconds -- and the
per-service locks are redirected to a tmp_path so nothing writes under /var/lock.

`ansible-playbook` is a stub that sleeps. Elapsed wall clock is therefore the whole signal:
two runs that overlap finish in about one sleep, two that serialize take two.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_service_lock_concurrency.py
"""

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from _deploy_sh_fakes import git_free_env, make_snapshot_repo

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_TREE_LOCK = "/var/lock/server-git-tree.lock"

# Long enough that the fixed costs (two git worktree adds, two bash startups) stay well under
# it, short enough that three cases cost under half a minute.
_SLEEP_S = 4

_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) sleep "$DEPLOY_TEST_SLEEP"; exit 0 ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture(autouse=True)
def _tree_lock_is_free():
    """Skip rather than queue when something real holds the tree lock.

    Every run here takes the real tree lock for its snapshot. A gitops tick holding it would
    make these runs wait on deploy.sh's own LOCK_WAIT budget, which is 50 minutes.
    """
    try:
        fd = os.open(_TREE_LOCK, os.O_WRONLY | os.O_CREAT, 0o666)
    except OSError as exc:
        pytest.skip(f"cannot open {_TREE_LOCK}: {exc}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pytest.skip(f"{_TREE_LOCK} is held by a real deploy")
    finally:
        os.close(fd)


def _harness(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(_UV_STUB)
    (bin_dir / "uv").chmod(0o755)
    locks = tmp_path / "locks"
    locks.mkdir()
    repo = make_snapshot_repo(tmp_path / "repo")
    env = git_free_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HOMELAB_DEPLOY_SNAPSHOT_ROOT=str(tmp_path / "snapshots"),
        HOMELAB_DEPLOY_LOCK_DIR=str(locks),
        DEPLOY_TEST_SLEEP=str(_SLEEP_S),
    )
    return repo, env


def _run_both(tmp_path: Path, first: list[str], second: list[str]) -> float:
    """Start two deploy.sh runs at once; seconds until both have finished."""
    repo, env = _harness(tmp_path)
    base = [str(_DEPLOY_SH), "--skip-tag-check", "--skip-staleness-check"]
    started = time.monotonic()
    procs = [
        subprocess.Popen(
            base + extra,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for extra in (first, second)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, f"{out}\n{err}"
    return time.monotonic() - started


def test_two_deploys_of_different_services_overlap(tmp_path):
    """CLEAN half, and the whole point of the change.

    Before ADR-0017 both runs held the tree lock across the playbook, so this took two sleeps.
    """
    elapsed = _run_both(tmp_path, ["--tags", "alpha"], ["--tags", "beta"])
    assert elapsed < 2 * _SLEEP_S, (
        f"two deploys of DIFFERENT services took {elapsed:.1f}s, which is at least two "
        f"{_SLEEP_S}s playbooks -- they serialized"
    )


def test_two_deploys_of_the_same_service_serialize(tmp_path):
    """FLAGGED half: without it the test above passes for a wrapper that takes no lock at all.

    Two deploys of one service race on the same manifests and the same rollout, which is what
    the per-service lock exists to prevent.
    """
    elapsed = _run_both(tmp_path, ["--tags", "alpha"], ["--tags", "alpha"])
    assert elapsed >= 2 * _SLEEP_S, (
        f"two deploys of the SAME service took {elapsed:.1f}s, under two {_SLEEP_S}s "
        "playbooks -- they overlapped"
    )


def test_a_full_run_and_a_scoped_run_serialize(tmp_path):
    """FLAGGED half for the `all` lock: a full run deploys the scoped run's service too.

    The scoped run takes `server-deploy-all.lock` shared and the full run takes it exclusive,
    so the two exclude each other while two scoped runs do not.
    """
    elapsed = _run_both(tmp_path, [], ["--tags", "alpha"])
    assert elapsed >= 2 * _SLEEP_S, (
        f"a full run and a scoped run took {elapsed:.1f}s, under two {_SLEEP_S}s playbooks "
        "-- they overlapped"
    )
