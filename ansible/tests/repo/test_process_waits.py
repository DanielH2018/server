"""`_process_waits.wait_for_exit` is a wait, so both verdicts need a proof."""

import os
import signal
import subprocess
import sys
import threading

import pytest
from _process_waits import wait_for_exit


def test_an_exited_process_is_reported_at_once() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert wait_for_exit(proc.pid, timeout=60) is True
    finally:
        proc.wait()


def test_a_reaped_pid_counts_as_exited() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert wait_for_exit(proc.pid, timeout=60) is True


def test_a_live_process_is_not_reported_before_the_deadline() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_for_exit(proc.pid, timeout=0.2) is False
    finally:
        proc.kill()
        proc.wait()


def test_a_reaped_group_leader_whose_member_survives_counts_as_exited() -> None:
    """The #5093 race, staged: reap a session leader while its child is still alive.

    The child holds the leader's number as its PGID/SID, so `pidfd_open` on that number
    raises EINVAL rather than ESRCH. `test_terminate_kills_the_pid` hit this when the
    `spawn_logged` reaper thread reaped `sh` before its SIGTERMed `sleep` was reaped.
    """
    leader = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo $!"],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert leader.stdout is not None
    member = int(leader.stdout.readline())
    leader.wait()
    leader.stdout.close()
    try:
        assert os.getpgid(member) == leader.pid, "the member left the leader's group"
        assert wait_for_exit(leader.pid, timeout=60) is True
    finally:
        os.kill(member, signal.SIGKILL)


def test_a_thread_id_is_an_error_not_an_exit() -> None:
    """EINVAL for a live non-leader thread is a wrong pid, and must not read as exited."""
    started = threading.Event()
    release = threading.Event()
    tids: list[int] = []

    def park() -> None:
        tids.append(threading.get_native_id())
        started.set()
        release.wait()

    thread = threading.Thread(target=park)
    thread.start()
    try:
        started.wait()
        with pytest.raises(OSError, match=r"\[Errno 22\] Invalid argument"):
            wait_for_exit(tids[0], timeout=0.2)
    finally:
        release.set()
        thread.join()


def test_a_non_positive_pid_is_an_error_not_an_exit() -> None:
    """`/proc/0` does not exist either, so the `/proc` test alone would call pid 0 exited."""
    with pytest.raises(OSError, match=r"\[Errno 22\] Invalid argument"):
        wait_for_exit(0, timeout=0.2)
