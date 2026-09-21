#!/usr/bin/env python3
"""Two `deploy.sh` runs overlap when they name different services, and not when they don't.

This is the property ADR-0017 exists for, and the only one that cannot be read off the script:
it is about two processes, so a single-process test would pass whatever the locks did. Every
lock is real, and all three paths -- the tree lock, the service locks and the snapshot root --
are redirected into a tmp_path, so these runs neither queue behind a live gitops tick nor make
one queue behind them.

`ansible-playbook` is a stub that sleeps. Elapsed wall clock is therefore the whole signal:
two runs that overlap finish in about one sleep, two that serialize take two.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_service_lock_concurrency.py
"""

import fcntl
import os
import select
import subprocess
import time
from pathlib import Path

from _deploy_sh_fakes import (
    FAKE_RECAP,
    UV_DEPLOY_LOCKS_ARM,
    deploy_sh_env,
    detached_pid,
    make_snapshot_repo,
)
from _process_waits import wait_for_exit

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

# The playbook stub's sleep. The FLAGGED halves below discriminate only while the fixed cost
# of a pair of runs (two git worktree adds, two bash startups) stays under one sleep: two
# runs that overlap take one sleep plus that cost, two that serialize take two sleeps plus it,
# and once the cost reaches a sleep the two are indistinguishable and a wrapper that took no
# lock at all would pass `..._serialize`. `test_the_fixed_cost_stays_under_half_a_sleep`
# measures the cost on the machine running the suite and fails before the halves can go
# vacuous. 2s, down from 4 (#2226): the cost measured 0.1s here and the runner is ~4x slower
# per test, so 2s keeps a 2x margin over the guard's own bound and halves a module that was
# the second pole of the sharded suite.
_SLEEP_S = 2

_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) sleep "$DEPLOY_TEST_SLEEP"; {recap}; exit 0 ;;
  *deploy_tags.py*) printf 'alpha\\nbeta\\n'; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_DEPLOY_LOCKS_ARM)

# The same stub, recording where the playbook was run from before it sleeps. `--detach` is the
# only arm whose cleanup happens after the parent has exited, so where it ran and what it left
# behind are both readable only from outside the process.
_UV_DETACH_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) pwd >"$DEPLOY_TEST_PWD_FILE"; sleep "$DEPLOY_TEST_SLEEP"; {recap}; exit 0 ;;
  *deploy_tags.py*) printf 'alpha\\nbeta\\n'; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_DEPLOY_LOCKS_ARM)


def _harness(
    tmp_path: Path, uv_stub: str = _UV_STUB, sleep_s: float = _SLEEP_S
) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(uv_stub)
    (bin_dir / "uv").chmod(0o755)
    repo = make_snapshot_repo(tmp_path / "repo")
    return repo, deploy_sh_env(tmp_path, bin_dir, DEPLOY_TEST_SLEEP=str(sleep_s))


def _run_both(
    tmp_path: Path, first: list[str], second: list[str], sleep_s: float = _SLEEP_S
) -> float:
    """Start two deploy.sh runs at once; seconds until both have finished."""
    repo, env = _harness(tmp_path, sleep_s=sleep_s)
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


def test_the_fixed_cost_stays_under_half_a_sleep(tmp_path):
    """Non-vacuity for the three wall-clock cases below, measured rather than assumed.

    With the stub's sleep at 0 the elapsed time of a pair is the fixed cost alone. Under half
    a sleep, the overlap case has a full sleep of headroom under its bound and the serialize
    cases cannot be satisfied by two runs that merely overlapped. A machine slow enough to
    fail this is one where the cases below would pass for the wrong reason, and this message
    is the one that says so.
    """
    cost = _run_both(tmp_path, ["--tags", "alpha"], ["--tags", "beta"], sleep_s=0)
    assert cost < _SLEEP_S / 2, (
        f"two zero-sleep deploys took {cost:.1f}s of fixed cost, at least half of the "
        f"{_SLEEP_S}s stub sleep, so the overlap and serialize cases below no longer "
        "discriminate -- raise _SLEEP_S"
    )


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


def _deploy(repo: Path, env: dict[str, str], *args: str) -> None:
    """One `deploy.sh` run that must succeed."""
    result = subprocess.run(
        [str(_DEPLOY_SH), "--skip-tag-check", "--skip-staleness-check", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_a_snapshot_whose_owner_lock_is_held_survives_another_runs_reap(tmp_path):
    """CLEAN half: a live snapshot is not collected, however dead its name's pid looks.

    `--detach` runs its playbook in a subshell whose `$$` is the parent's pid, and the parent
    exits immediately — so under the pid-based reaper this directory was deleted out from under
    a running deploy by the next invocation of anything, `--check` included.
    """
    repo, env = _harness(tmp_path)
    env["DEPLOY_TEST_SLEEP"] = "0"
    live = tmp_path / "snapshots" / "beta-20260911-000000-999999"
    live.mkdir(parents=True)
    fd = os.open(live / ".deploy-owner.lock", os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _deploy(repo, env, "--tags", "alpha")
        assert live.is_dir(), (
            "a deploy reaped a snapshot whose owner still holds its lock"
        )
    finally:
        os.close(fd)

    # FLAGGED half: with the owner gone the same directory MUST go, or a crashed run leaks a
    # worktree forever and the reaper is decoration.
    _deploy(repo, env, "--tags", "alpha")
    assert not live.exists(), "a snapshot with no live owner was left behind"


def test_an_unlocked_invocation_reaps_nothing_while_the_tree_lock_is_held(tmp_path):
    """The window between `git worktree add` and the owner-lock flock, closed by the tree lock.

    For those seconds the directory exists with no owner, and a `--check` run — which takes no
    lock at all — would have reaped a worktree another process was in the middle of creating.
    Reaping moved inside the tree lock, so `--check` no longer reaps; this plants exactly that
    ownerless directory, holds the tree lock the way the creating process would, and asserts a
    concurrent `--check` leaves it alone.
    """
    repo, env = _harness(tmp_path)
    env["DEPLOY_TEST_SLEEP"] = "0"
    being_made = tmp_path / "snapshots" / "alpha-20260911-000000-424242"
    being_made.mkdir(parents=True)

    tree_lock = os.open(
        env["HOMELAB_DEPLOY_TREE_LOCK"], os.O_WRONLY | os.O_CREAT, 0o666
    )
    try:
        fcntl.flock(tree_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _deploy(repo, env, "--check", "--tags", "alpha")
        assert being_made.is_dir(), (
            "--check reaped a snapshot directory whose owner had not flocked it yet"
        )
    finally:
        os.close(tree_lock)

    # CLEAN half for the reaper itself: once the tree lock is free, a locked run collects it.
    _deploy(repo, env, "--tags", "alpha")
    assert not being_made.exists(), "the ownerless directory was never collected"


def test_a_live_detached_snapshot_survives_a_concurrent_check_and_deploy(tmp_path):
    """The end-to-end shape of C-1, with nothing simulated: two real runs against a live one.

    `--check` takes no tree lock and so reaps nothing; a scoped deploy of another service reaps
    under the tree lock and must leave this snapshot alone, because its owner still holds the
    lock inside it. Under the pid-based reaper either one deleted the worktree the detached
    playbook was rendering from, and the operator was told "retrying alone will not fix either".
    """
    repo, env = _harness(tmp_path, uv_stub=_UV_DETACH_STUB)
    pwd_fifo, pwd_fd = _pwd_fifo(tmp_path)
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_fifo)
    # Long enough for a `--check` and a scoped deploy to run inside it, and no longer: the
    # assertions below are on state — the directory still exists, then it does not — rather
    # than on elapsed time, and `--dist loadscope` keeps this whole module on one worker.
    env["DEPLOY_TEST_SLEEP"] = "3"
    output = tmp_path / "detach-output"
    with output.open("w") as sink:
        detached = subprocess.run(
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
    assert detached == 0, output.read_text()
    snapshot = _playbook_cwd(pwd_fd)

    # Both concurrent runs finish immediately: only the detached playbook sleeps.
    quick = dict(env, DEPLOY_TEST_SLEEP="0")
    _deploy(repo, quick, "--check", "--tags", "beta")
    _deploy(repo, quick, "--tags", "beta")
    assert snapshot.is_dir(), (
        f"{snapshot} was reaped while the detached playbook was still rendering from it"
    )

    # And it is the OWNER that cleans up, once the playbook it is running finishes.
    assert wait_for_exit(detached_pid(output.read_text())), (
        "the detached subshell never finished"
    )
    assert not snapshot.exists(), "the detached run left its snapshot behind"
    os.close(pwd_fd)


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


def _pwd_fifo(tmp_path: Path) -> tuple[Path, int]:
    """A fifo for the playbook stub's `pwd`, and a descriptor on it this test holds.

    Opened O_RDWR, so the descriptor is itself a writer: the pipe never reads as closed
    before the stub has opened it, and `select` in `_playbook_cwd` fires on the stub's line
    and on nothing else. That line is the signal the backgrounded playbook has started.

    Keep the descriptor open until every run that inherits the fifo's path has finished. A
    stub's `pwd >` blocks in open() while the fifo has no reader, and this descriptor is the
    reader; a later run whose line nobody reads writes into the pipe buffer and moves on.
    """
    path = tmp_path / "playbook-pwd"
    os.mkfifo(path)
    return path, os.open(path, os.O_RDWR)


def _playbook_cwd(fd: int, timeout: float = 60) -> Path:
    """The directory the backgrounded playbook stub ran from, once it has run."""
    readable, _, _ = select.select([fd], [], [], timeout)
    assert readable, "the backgrounded playbook never ran"
    return Path(os.read(fd, 4096).decode().strip())


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
    pwd_fifo, pwd_fd = _pwd_fifo(tmp_path)
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_fifo)
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

    playbook_cwd = _playbook_cwd(pwd_fd)
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

    # The subshell releases the lock and removes the snapshot on its way out, so its exit is
    # the point after which both must hold.
    assert wait_for_exit(detached_pid(output.read_text())), (
        "the detached subshell never finished"
    )
    assert _service_lock_free(alpha_lock), (
        "the detached run never released its service lock"
    )
    assert not any(snapshots.iterdir()), (
        f"the detached run left its snapshot behind in {snapshots}"
    )
    os.close(pwd_fd)
