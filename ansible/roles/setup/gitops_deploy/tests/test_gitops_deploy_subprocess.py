"""How gitops_deploy.py stops what it exec'd.

`run()`'s timeout must kill the whole process group or a wedged ansible-playbook outlives the
tick. The broad apply's argv is pinned in test_gitops_deploy_main_branches.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_subprocess.py

import os
import select
import subprocess
import time

import pytest

from lib.proc_testing import write_exec

from deployer_lib import deploy_io
from _process_waits import wait_for_exit

# The checkout the play would run from; every argv assertion below is about --tags, not cwd.
REPO = "/tmp/gitops-test-repo"


# ── run()'s timeout must kill the whole process group ───────────────────────────────────────────
# `uv run ansible-playbook ...` is a GRANDCHILD of run()'s subprocess (uv forks it rather than
# exec'ing into it). `subprocess.run(timeout=)` DOES return promptly on timeout — its internal
# communicate() raises on the wall-clock deadline, not on pipe EOF — but it kills only the DIRECT
# child (uv). Verified empirically against the pre-fix implementation: the call returns on time
# and the grandchild is still alive at that moment, left running as an orphan with nothing
# watching it. That is how a phase timeout stopped being an actual bound on the underlying
# ansible-playbook: gitops_deploy.py moves on while the timed-out run keeps mutating the host,
# and the real stop becomes systemd's TimeoutStartSec SIGTERM against the wrapping unit. This shape reproduces it directly: a shell script backgrounds a grandchild
# that outlives a naive kill-the-direct-child-only fix, so the test fails against the OLD run()
# and passes only once the whole process group is killed.
_GRANDCHILD_SHAPE = """#!/bin/sh
sh -c 'echo $$ > "{pidfifo}"; sleep 300' &
wait
"""


def test_run_timeout_kills_the_whole_process_group(tmp_path) -> None:
    # The grandchild announces its pid down a fifo. Opened O_RDWR here, before the script
    # starts: a write end this test holds keeps the pipe from reading as closed while the
    # grandchild has not opened it yet, so `select` fires on the pid line and on nothing else.
    pidfifo = tmp_path / "grandchild.pid"
    os.mkfifo(pidfifo)
    pidfd = os.open(pidfifo, os.O_RDWR)
    script = write_exec(
        tmp_path / "parent.sh", _GRANDCHILD_SHAPE.format(pidfifo=pidfifo)
    )

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        deploy_io.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)
    elapsed = time.monotonic() - start

    # Both the buggy and the fixed run() return around the 1.0s deadline — the deadline is a
    # wall-clock check inside communicate(), not a wait for pipe EOF, so this alone does not
    # discriminate them. It is here as a sanity bound; the real regression check is the
    # grandchild-liveness assert below.
    assert elapsed < 10, (
        f"run() took {elapsed:.1f}s to return after a 1.0s timeout — expected it to return "
        f"around the deadline regardless of whether the fix is applied"
    )

    try:
        readable, _, _ = select.select([pidfd], [], [], 60)
        assert readable, "the grandchild never started"
        grandchild_pid = int(os.read(pidfd, 64).split()[0])
    finally:
        os.close(pidfd)

    # A pidfd reads as terminated the moment the grandchild exits, before anything reaps it,
    # so the wait is on the kill itself rather than on the reparent-and-reap that follows.
    # The deadline must stay well under the grandchild's own `sleep 300`: a grandchild the
    # buggy run() left alive has to be seen alive, not caught exiting on its own.
    assert wait_for_exit(grandchild_pid, timeout=30), (
        f"grandchild pid {grandchild_pid} outlived the timeout — only the direct child was "
        f"killed, not its process group"
    )


# ── a timed-out run must still name the task it was running ──────────────────────────────────
# `subprocess.TimeoutExpired.__str__` prints the argv and the deadline and nothing else, so a
# wedged broad apply would log exactly `Command [...] timed out after 1800 seconds` — neither
# the running task nor a PLAY RECAP. The broad arm is forward-only, so that line would be all
# the operator had. RED proof: against a `run()` that
# re-raises the stdlib exception unchanged, the message below carries no TASK header at all,
# even though the playbook printed one before it wedged.
_WEDGED_TASK_SHAPE = """#!/bin/sh
echo 'TASK [manifests : apply rendered manifests] ***'
echo 'changed: [daniel-box]'
sleep 300
"""

_TASK_HEADER = "TASK [manifests : apply rendered manifests]"


def test_run_timeout_error_names_the_task_that_was_still_running(tmp_path) -> None:
    script = write_exec(tmp_path / "wedged.sh", _WEDGED_TASK_SHAPE)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        deploy_io.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)

    message = str(caught.value)
    assert _TASK_HEADER in message, (
        f"the timeout error must name the running task; got: {message!r}"
    )
    # The stdlib's own text stays: the argv and the deadline are what say it was a timeout
    # rather than a failure, and `deploy_handlers` logs this string and nothing else.
    assert "timed out after" in message
