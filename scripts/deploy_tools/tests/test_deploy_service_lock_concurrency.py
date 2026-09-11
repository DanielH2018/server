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

# The same stub, recording where the playbook was run from before it sleeps. `--detach` is the
# only arm whose cleanup happens after the parent has exited, so where it ran and what it left
# behind are both readable only from outside the process.
_UV_DETACH_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) pwd >"$DEPLOY_TEST_PWD_FILE"; sleep "$DEPLOY_TEST_SLEEP"; exit 0 ;;
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


def _harness(tmp_path: Path, uv_stub: str = _UV_STUB) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(uv_stub)
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


def _service_lock_free(path: Path) -> bool:
    """Is nothing holding this service lock? Takes and releases it non-blocking."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _wait_for(predicate, limit: float):
    """Poll `predicate` until it is true or `limit` seconds pass; the final verdict."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_detached_deploy_holds_its_lock_and_its_snapshot_until_the_playbook_ends(
    tmp_path,
):
    """The `--detach` arm's own invariant, which no other test reaches.

    The parent returns while the playbook is still running, so three things have to survive the
    fork: the service lock (held on the inherited open file description, which the parent's own
    close cannot release), the snapshot directory (the parent's EXIT trap fires the instant it
    backgrounds the job, so the cleanup has to belong to the subshell), and the cleanup itself.
    Deleting the snapshot too early and leaking it are both invisible to the run that did it.
    """
    repo, env = _harness(tmp_path, uv_stub=_UV_DETACH_STUB)
    pwd_file = tmp_path / "playbook-pwd"
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_file)
    snapshots = tmp_path / "snapshots"
    alpha_lock = tmp_path / "locks" / "server-deploy-alpha.lock"

    # Output goes to a FILE, not a pipe. The backgrounded subshell inherits the parent's
    # stdout, so a pipe stays open until the playbook ends and `capture_output` would wait for
    # exactly the thing this test is checking the parent does not wait for.
    output = tmp_path / "detach-output"
    started = time.monotonic()
    with output.open("w") as sink:
        returncode = subprocess.run(
            [
                str(_DEPLOY_SH),
                "--detach",
                "--tags",
                "alpha",
                "--skip-tag-check",
                "--skip-staleness-check",
            ],
            cwd=repo,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        ).returncode
    assert returncode == 0, output.read_text()
    assert time.monotonic() - started < _SLEEP_S, (
        "--detach waited for the playbook instead of backgrounding it"
    )

    assert _wait_for(pwd_file.exists, _SLEEP_S), "the backgrounded playbook never ran"
    playbook_cwd = Path(pwd_file.read_text().strip())
    assert snapshots in playbook_cwd.parents, (
        f"the detached playbook ran from {playbook_cwd}, not from a snapshot under {snapshots}"
    )
    assert playbook_cwd.is_dir(), (
        "the parent deleted the snapshot out from under the playbook"
    )
    assert not _service_lock_free(alpha_lock), (
        "the detached run released its service lock while the playbook was still running, so "
        "another deploy of alpha could start on top of it"
    )

    assert _wait_for(lambda: _service_lock_free(alpha_lock), 60), (
        "the detached run never released its service lock"
    )
    assert _wait_for(lambda: not any(snapshots.iterdir()), 10), (
        f"the detached run left its snapshot behind in {snapshots}"
    )
