"""`_process_waits.wait_for_exit` is a wait, so both verdicts need a proof."""

import subprocess
import sys

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
