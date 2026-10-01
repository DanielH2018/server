"""`lib/detach_fork.py`: the double fork and the move out of a user-manager unit's cgroup.

Run: uv run pytest scripts/lib/tests/test_detach_fork.py

The end-to-end halves live with their callers: `test_land_detach.py` stops a real systemd user
unit under a landing, and `test_deploy_runs_from_a_snapshot_under_service_locks.py` pins `deploy_detach.run` to `fork_detached`.
"""

import os
import subprocess
import time

import pytest
from _process_waits import wait_for_exit

from lib import detach_fork

_FANOUT = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/fanout-3159-3160.service\n"


def test_the_detached_run_is_not_a_child_of_its_caller(tmp_path):
    """Issue #3159: a single fork leaves the run a child, so a kill of the caller's tree reaches it."""
    marker = tmp_path / "ran"

    def body():
        marker.write_text("ok")
        time.sleep(0.5)
        os._exit(0)

    pid = detach_fork.fork_detached(body)
    try:
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert os.getsid(pid) != os.getsid(0)
    finally:
        assert wait_for_exit(pid, timeout=10)
    assert marker.read_text() == "ok"


def test_a_fan_out_units_cgroup_is_flagged():
    assert detach_fork.unit_of(_FANOUT) == "fanout-3159-3160.service"


@pytest.mark.parametrize(
    "cgroup",
    [
        # A system unit: leaving it needs root, and the deployer waits for its own deploys.
        "0::/system.slice/gitops-deploy.service\n",
        # A login session's scope, and a scope this module already moved the run into.
        "0::/user.slice/user-1000.slice/session-4.scope\n",
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/land3137-99.scope\n",
        # The user manager's own process, which is not inside any service under it.
        "0::/user.slice/user-1000.slice/user@1000.service/init.scope\n",
    ],
)
def test_a_cgroup_outside_a_user_service_is_clean(cgroup):
    assert detach_fork.unit_of(cgroup) is None


def test_the_scope_name_carries_the_pid_and_only_unit_name_characters():
    assert detach_fork.scope_name("land3137", 42) == "land3137-42.scope"
    assert detach_fork.scope_name("deploy+a b", 7) == "deploy_a_b-7.scope"


def _completed(rc: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout="", stderr=stderr)


def test_outside_a_user_service_nothing_is_asked_of_systemd():
    calls: list[list[str]] = []
    line = detach_fork.leave_unit_cgroup(
        "land1",
        read_cgroup=lambda: "0::/system.slice/gitops-deploy.service\n",
        run=lambda argv: calls.append(argv) or _completed(0),
    )
    assert line is None
    assert calls == []


def test_inside_a_user_service_the_run_moves_into_its_own_scope():
    scope = detach_fork.scope_name("land1", os.getpid())
    reads = iter(
        [_FANOUT, _FANOUT, f"0::/user.slice/user@1000.service/app.slice/{scope}\n"]
    )
    calls: list[list[str]] = []
    line = detach_fork.leave_unit_cgroup(
        "land1",
        read_cgroup=lambda: next(reads),
        run=lambda argv: calls.append(argv) or _completed(0),
        sleep=lambda _s: None,
    )
    [argv] = calls
    assert argv[:3] == ["busctl", "--user", "call"]
    assert argv[argv.index("StartTransientUnit") + 2] == scope
    assert argv[argv.index("PIDs") + 3] == str(os.getpid())
    assert line is not None
    assert line.startswith("detach: moved out of fanout-3159-3160.service")
    assert f"systemctl --user stop {scope}" in line


def test_a_refused_move_fails_open_and_says_the_unit_still_owns_the_run():
    line = detach_fork.leave_unit_cgroup(
        "deploy",
        read_cgroup=lambda: _FANOUT,
        run=lambda _argv: _completed(1, "Failed to connect to bus"),
    )
    assert line == (
        "detach: could not leave fanout-3159-3160.service (Failed to connect to bus); "
        "stopping it stops this run."
    )


def test_a_move_that_never_shows_gives_up_on_its_budget():
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    line = detach_fork.leave_unit_cgroup(
        "deploy",
        read_cgroup=lambda: _FANOUT,
        run=lambda _argv: _completed(0),
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert line is not None
    assert "had not shown after 5s" in line
