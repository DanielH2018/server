#!/usr/bin/env python3
"""`deploy.sh --detach` holds its service locks until its health gate has read the workloads.

The gate reads one `probe.py health` sample per tag. A lock released before it lets a second
deploy of the same service roll a `Recreate` workload under that sample, and the detached run
then posts `unhealthy` about a healthy change (#3817).

The probe below takes the lock the way a second deploy does: through its own open file
description, non-blocking, so a held lock answers at once instead of hanging the test.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_detach_gate_holds_locks.py
"""

import fcntl
import os
from pathlib import Path

from deploy_tools import deploy_detach
from deploy_tools import deploy_under_locks as locked


def _another_deploy_could_take(lock: Path) -> bool:
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def _locked_run(tmp_path: Path) -> tuple[locked.Run, Path]:
    lock = tmp_path / "sonarr.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    run = locked.Run(repo_root=tmp_path, tags=["sonarr"], at_sha="", args=[])
    run.service_fds = [fd]
    return run, lock


def test_the_service_lock_is_held_while_the_gate_runs(tmp_path):
    run, lock = _locked_run(tmp_path)
    seen = []
    steps = deploy_detach.ChildSteps(
        run_playbook=lambda _run: 0,
        annotate=lambda _run: None,
        notify=lambda *_args: seen.append(_another_deploy_could_take(lock)),
    )
    deploy_detach.deploy_and_gate(run, tmp_path / "log", "notifier.py", steps)
    assert seen == [False], "a second deploy could take the lock under the gate"
    run.close()
    assert _another_deploy_could_take(lock), "the lock outlived the run"
