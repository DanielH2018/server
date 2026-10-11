#!/usr/bin/env python3
"""What survives a `deploy.sh` run forking: its snapshot, its lock and its cleanup.

Every lock is real, and all three paths -- the tree lock, the service locks and the snapshot
root -- are redirected into a tmp_path, so these runs neither queue behind a live gitops tick
nor make one queue behind them. `ansible-playbook` is a stub, and no arm of it sleeps: the
three `--detach` tests park it on a gate fifo, which is what gives a `--detach` parent
something to return in front of, and the test opens the gate when it has read what it came
for. Nothing here asserts on elapsed time, so nothing here loses a race with a sleep that
ended early (issue #3171, issue #3173).

THE SERIALIZE/OVERLAP PROPERTY IS NOT MEASURED HERE. Timing two real deploys at about 10s a
run would be the only way to prove "same service waits, different service does not, a full run
excludes a scoped one" by wall clock, so each half is pinned cheaply somewhere that reads the
same code:

- A busy service lock really blocking `deploy.sh`, cross-process, with a real external `flock`
  holder: `test_wrapper_lock_wait_lines.py` (the wait line it prints, and exit 75 when the
  budget ends).
- The lock list and its order, driven through `take_service_locks` and read back off
  /proc/self/fd, plus the `all` lock's shared/exclusive modes:
  `ansible/tests/deploy/test_deploy_sh_takes_the_locks_deploy_locks_plans.py`.
- The lock semantics themselves, on real fcntl locks:
  `ansible/roles/setup/gitops_deploy/tests/test_deploy_service_locks.py`.

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
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    detached_pid,
    make_snapshot_repo,
    stub_bin,
)
from _process_waits import wait_for_exit

from lib.repo_paths import REPO as _REPO

_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

# The plain stub: the playbook returns at once. The two reaper tests below plant a snapshot
# directory themselves and read the verdict a FINISHED run reached on it, so they need a run
# that ends rather than a window to look through.
_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
  *deploy_tags.py*) printf 'alpha\\nbeta\\n'; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)

# The same again, parked on a gate fifo instead of a sleep. A test that reads state which only
# holds WHILE the playbook runs -- the service lock, the snapshot, the child's descriptors --
# bets with a sleep that it is scheduled inside those seconds, and issue #3171 is the full
# suite losing that bet once under four xdist workers and winning the immediate retry. The
# stub's `read` blocks until the test writes to the gate, so the window is as long as the
# assertions take and the sleep's wall clock leaves the test entirely.
#
# ONLY the `alpha` run parks. One gate fifo is consumed by whichever run reaches it first, so
# a test that runs several playbooks under one stub needs the gate scoped to the run whose
# window it is reading -- `alpha` for the detached run, with every other service answering
# immediately (issue #3173). The quotes make `--tags alpha` one case pattern rather than two
# words, and `deploy_lib/run.py` leaves the flag and its value adjacent in the playbook's argv.
# Writing the `pwd` in the parking arm alone keeps a pattern miss deterministic: no run writes
# the fifo, and `_playbook_cwd` fails on its deadline instead of degrading to a race.
_UV_GATED_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*"--tags alpha"*) pwd >"$DEPLOY_TEST_PWD_FILE"; read -r _ <"$DEPLOY_TEST_GATE_FILE"; {recap}; exit 0 ;;
  *ansible-playbook*) {recap}; exit 0 ;;
  *deploy_tags.py*) printf 'alpha\\nbeta\\n'; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)


def _harness(tmp_path: Path, uv_stub: str = _UV_STUB) -> tuple[Path, dict[str, str]]:
    bin_dir = stub_bin(
        tmp_path,
        {
            "uv": uv_stub,
        },
    )
    repo = make_snapshot_repo(tmp_path / "repo")
    return repo, deploy_sh_env(tmp_path, bin_dir)


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
    exits immediately — so a pid-based reaper would delete this directory out from under a
    running deploy at the next invocation of anything, `--check` included.
    """
    repo, env = _harness(tmp_path)
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
    Reaping happens inside the tree lock, so `--check` does not reap; this plants exactly that
    ownerless directory, holds the tree lock the way the creating process would, and asserts a
    concurrent `--check` leaves it alone.
    """
    repo, env = _harness(tmp_path)
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

    The detached `alpha` playbook parks on the gate and the two `beta` runs answer immediately,
    so the snapshot is live for exactly as long as those two runs plus these assertions take.
    Against a sleep this was a bet that both inner runs finished inside it (issue #3173).
    """
    repo, env = _harness(tmp_path, uv_stub=_UV_GATED_STUB)
    pwd_fifo, pwd_fd = _pwd_fifo(tmp_path)
    gate_fifo, gate_fd = _gate_fifo(tmp_path)
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_fifo)
    env["DEPLOY_TEST_GATE_FILE"] = str(gate_fifo)
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

    # Both concurrent runs finish immediately: only the `alpha` playbook parks on the gate.
    _deploy(repo, env, "--check", "--tags", "beta")
    _deploy(repo, env, "--tags", "beta")
    assert snapshot.is_dir(), (
        f"{snapshot} was reaped while the detached playbook was still rendering from it"
    )

    # And it is the OWNER that cleans up, once the playbook it is running finishes.
    _open_gate(gate_fd)
    assert wait_for_exit(detached_pid(output.read_text())), (
        "the detached subshell never finished"
    )
    assert not snapshot.exists(), "the detached run left its snapshot behind"
    os.close(pwd_fd)
    os.close(gate_fd)


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


def _fifo(path: Path) -> tuple[Path, int]:
    """`path` as a fifo, and a descriptor on it this test holds.

    Opened O_RDWR, so the descriptor is both a reader and a writer. Neither end ever reads as
    closed while the test holds it: the stub's `>` and `<` on the same path both open without
    blocking, whichever runs first, and a `select` on this descriptor fires on what the stub
    wrote and on nothing else.
    """
    os.mkfifo(path)
    return path, os.open(path, os.O_RDWR)


def _pwd_fifo(tmp_path: Path) -> tuple[Path, int]:
    """The fifo the playbook stub writes its `pwd` to. That line says the playbook started.

    Keep the descriptor open until every run that inherits the fifo's path has finished. A
    stub's `pwd >` blocks in open() while the fifo has no reader, and this descriptor is the
    reader; a later run whose line nobody reads writes into the pipe buffer and moves on.
    """
    return _fifo(tmp_path / "playbook-pwd")


def _gate_fifo(tmp_path: Path) -> tuple[Path, int]:
    """The fifo `_UV_GATED_STUB` parks on, and the descriptor `_open_gate` releases it with."""
    return _fifo(tmp_path / "playbook-gate")


def _open_gate(fd: int) -> None:
    """Let the parked playbook stub finish. The test's own descriptor is a reader too, so this
    write never blocks, even when nothing is parked on the gate at all."""
    os.write(fd, b"go\n")


def _playbook_cwd(fd: int, timeout: float = 60) -> Path:
    """The directory the backgrounded playbook stub ran from, once it has run."""
    readable, _, _ = select.select([fd], [], [], timeout)
    assert readable, "the backgrounded playbook never ran"
    return Path(os.read(fd, 4096).decode().strip())


def _held_fds(pid: int) -> dict[int, str]:
    """Every descriptor `pid` holds, by number. A descriptor closed mid-listing is skipped."""
    held = {}
    for link in Path(f"/proc/{pid}/fd").iterdir():
        try:
            held[int(link.name)] = os.readlink(link)
        except OSError:
            continue
    return held


def _settled_fds(pid: int, timeout: float = 10) -> dict[int, str]:
    """`pid`'s descriptors once only stdio and locks are left, else the last sample taken.

    `run_playbook` hands the playbook's stdout to a `tee` child through a pipe it closes in a
    `finally` that runs AFTER the playbook is spawned, so for the microseconds either side of
    the signal a test waits on -- the stub's first line -- the child legitimately holds two
    pipes. Measured under 0.1ms on daniel-server and never caught by a sample, but it is a
    race, and polling for the settled set costs nothing.

    This weakens no assertion: a descriptor the child never closes, which is the leak of issue
    #3162, never settles, and the deadline then hands the last sample to the assertion that
    names it. The caller asserts on the returned sample rather than on this returning.
    """
    deadline = time.monotonic() + timeout
    while True:
        held = _held_fds(pid)
        beyond_stdio = set(held) - {0, 1, 2}
        locks = {fd for fd, target in held.items() if target.endswith(".lock")}
        if locks and beyond_stdio == locks:
            return held
        if time.monotonic() >= deadline:
            return held
        time.sleep(0.01)


def test_a_detached_deploy_holds_its_lock_and_its_snapshot_until_the_playbook_ends(
    tmp_path,
):
    """The `--detach` arm's own invariant, which no other test reaches.

    The parent returns while the playbook is still running, so three things have to survive the
    fork: the service lock (held on the inherited open file description, which the parent's own
    close cannot release), the snapshot directory (the parent's EXIT trap fires the instant it
    backgrounds the job, so the cleanup has to belong to the subshell), and the cleanup itself.
    Deleting the snapshot too early and leaking it are both invisible to the run that did it.

    The playbook parks on the gate rather than sleeping, so the parent returning at all is the
    proof it backgrounded the job, and every assertion below runs with the playbook still
    inside its run however loaded the host is.
    """
    repo, env = _harness(tmp_path, uv_stub=_UV_GATED_STUB)
    pwd_fifo, pwd_fd = _pwd_fifo(tmp_path)
    gate_fifo, gate_fd = _gate_fifo(tmp_path)
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_fifo)
    env["DEPLOY_TEST_GATE_FILE"] = str(gate_fifo)
    snapshots = tmp_path / "snapshots"
    alpha_lock = tmp_path / "locks" / "server-deploy-alpha.lock"

    # Output goes to a FILE, not a pipe. The backgrounded subshell inherits the parent's
    # stdout, so a pipe stays open until the playbook ends and `capture_output` would wait for
    # exactly the thing this test is checking the parent does not wait for.
    output = tmp_path / "detach-output"
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
    assert returncode == 0, (
        "--detach waited for the parked playbook instead of backgrounding it, or refused:\n"
        + output.read_text()
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
    _open_gate(gate_fd)
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
    os.close(gate_fd)


def test_a_detached_deploy_keeps_only_stdio_its_log_and_its_locks(tmp_path):
    """Issue #3162: the detached child drops every descriptor its caller passed it.

    An inherited pipe of the caller's -- a harness's output capture -- keeps the caller's call
    open for as long as the playbook runs. This passes `deploy.sh` a stray descriptor on a
    marker file and reads the child's own /proc/<pid>/fd while the playbook is still inside its
    run: the marker must be gone, and the service lock and the snapshot's owner lock must still
    be held.

    The playbook parks on the gate rather than sleeping (issue #3171). Read against a sleep,
    this listing is a bet that the test is scheduled before the sleep ends -- and the service
    lock the assertions want is closed the instant the playbook returns, so losing that bet
    reads as the invariant being broken rather than as a sample taken too late.
    """
    repo, env = _harness(tmp_path, uv_stub=_UV_GATED_STUB)
    pwd_fifo, pwd_fd = _pwd_fifo(tmp_path)
    gate_fifo, gate_fd = _gate_fifo(tmp_path)
    env["DEPLOY_TEST_PWD_FILE"] = str(pwd_fifo)
    env["DEPLOY_TEST_GATE_FILE"] = str(gate_fifo)
    stray_path = tmp_path / "callers-stray-descriptor"
    stray = os.open(stray_path, os.O_WRONLY | os.O_CREAT, 0o644)
    output = tmp_path / "detach-output"
    try:
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
                pass_fds=[stray],
                timeout=120,
                check=False,
            ).returncode
    finally:
        os.close(stray)
    assert returncode == 0, output.read_text()
    snapshot = _playbook_cwd(pwd_fd)
    pid = detached_pid(output.read_text())

    held = _settled_fds(pid)
    assert str(stray_path) not in held.values(), (
        f"the detached child still holds its caller's descriptor on {stray_path}: {held}"
    )
    # The control: the descriptors the child needs are read off the same listing.
    assert str(tmp_path / "locks" / "server-deploy-alpha.lock") in held.values(), held
    assert str(snapshot / ".deploy-owner.lock") in held.values(), held
    assert set(held) - {0, 1, 2} == {
        fd for fd, target in held.items() if target.endswith(".lock")
    }, f"the detached child holds descriptors beyond stdio and its locks: {held}"

    _open_gate(gate_fd)
    assert wait_for_exit(pid), "the detached child never finished"
    os.close(pwd_fd)
    os.close(gate_fd)
