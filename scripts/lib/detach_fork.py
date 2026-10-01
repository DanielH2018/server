"""Run work that outlives its caller: `land.sh --detach` and `deploy.sh --detach` share this.

Two different kills can reach a detached run, and each half of this module escapes one.

THE CALLER'S PROCESS TREE (issues #3158, #3159). A harness that times out a Bash call kills the
call's whole descendant tree, found by walking parent pids. A child in its own session
(`setsid`) is still a descendant while its parent lives. `fork_detached` forks twice: the
intermediate child calls `setsid`, forks the worker and exits at once. The worker reparents to
init or the nearest subreaper -- `systemd --user` on daniel-box -- outside the caller's tree.

THE CALLER'S CGROUP (issue #3160). A fan-out agent runs inside a systemd user unit
(`fanout-<n>.service`), and every process it starts sits in that unit's cgroup whatever its
fork shape. `systemctl --user stop` kills the whole cgroup, so a landing died mid-deploy with
nothing recording that it stopped. `leave_unit_cgroup` moves the calling process into its own
transient scope through the user manager's `StartTransientUnit` -- what `systemd-run --scope`
does for a command it starts, applied to a process that is already running. Stopping the
fan-out unit then leaves the work running. Its way out is `systemctl --user stop <scope>`, and
the line this writes into the run's log names the scope.

Only a `.service` under `user@<uid>.service` is left. A system unit such as
`gitops-deploy.service` is left alone on purpose: moving out of it needs root, and the deployer
waits for its own deploys. A login session's scope is left alone too.
"""

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# How long the move may take to show in /proc/self/cgroup. `StartTransientUnit` returns a job
# rather than waiting for it; the move measured under 50ms on daniel-box.
MOVE_WAIT_S = 5.0
MOVE_POLL_S = 0.05
BUSCTL_TIMEOUT_S = 10

# The cgroup v2 line of a process inside a user manager's service, and that service's name.
_USER_SERVICE = re.compile(r"^0::/.*/user@\d+\.service/(?:.+/)?([^/]+\.service)$")


def fork_detached(body: Callable[[], None]) -> int:
    """Run `body` in a grandchild in its own session; its pid, in the caller.

    `body` should end with `os._exit`. If it returns or raises, the grandchild exits 1 rather
    than unwinding the caller's frames a second time.

    Returns:
      The grandchild's pid. Only the caller ever returns from this function.
    """
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid:
        os.close(write_end)
        with os.fdopen(read_end, "rb") as pipe:
            reported = pipe.read()
        os.waitpid(pid, 0)
        if not reported:
            raise RuntimeError("detach: the detached run never started (fork failed)")
        return int(reported)
    try:
        os.close(read_end)
        os.setsid()
        grandchild = os.fork()
        if grandchild:
            os.write(write_end, str(grandchild).encode())
            os._exit(0)
        os.close(write_end)
        body()
    finally:
        os._exit(1)


def unit_of(cgroup_text: str) -> str | None:
    """The user-manager service a process runs in, from its /proc/<pid>/cgroup; else None."""
    for line in cgroup_text.splitlines():
        match = _USER_SERVICE.match(line.strip())
        if match:
            return match.group(1)
    return None


def scope_name(prefix: str, pid: int) -> str:
    """`<prefix>-<pid>.scope`, with anything systemd refuses in a unit name made `_`."""
    return f"{re.sub(r'[^A-Za-z0-9:_.-]', '_', prefix)}-{pid}.scope"


def start_scope_argv(scope: str, pid: int) -> list[str]:
    """The `busctl` call that creates `scope` holding `pid`, collected once it is empty."""
    return [
        "busctl",
        "--user",
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        scope,
        "fail",
        "2",
        "PIDs",
        "au",
        "1",
        str(pid),
        "CollectMode",
        "s",
        "inactive-or-failed",
        "0",
    ]


def _read_own_cgroup() -> str:
    return Path("/proc/self/cgroup").read_text()


def _busctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=BUSCTL_TIMEOUT_S,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def leave_unit_cgroup(
    prefix: str,
    read_cgroup: Callable[[], str] = _read_own_cgroup,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _busctl,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Move this process out of its user-manager service into `<prefix>-<pid>.scope`.

    Call it before starting any subprocess: a child started while the move is pending stays in
    the unit's cgroup. Fails open. The run goes ahead where it is, and the returned line says
    why, because a run that cannot leave is no worse off than before this existed.

    Returns:
      A line for the run's log, or None when the process is in no user-manager service.
    """
    try:
        unit = unit_of(read_cgroup())
    except OSError:
        return None
    if unit is None:
        return None
    pid = os.getpid()
    scope = scope_name(prefix, pid)
    try:
        result = run(start_scope_argv(scope, pid))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"detach: could not leave {unit} ({exc}); stopping it stops this run."
    if result.returncode != 0:
        why = (
            result.stderr or result.stdout
        ).strip() or f"busctl exit {result.returncode}"
        return f"detach: could not leave {unit} ({why}); stopping it stops this run."
    deadline = clock() + MOVE_WAIT_S
    while True:
        try:
            if f"/{scope}" in read_cgroup():
                return (
                    f"detach: moved out of {unit} into {scope}; stopping {unit} leaves this "
                    f"run going. To stop it: systemctl --user stop {scope}"
                )
        except OSError:
            pass
        if clock() >= deadline:
            return (
                f"detach: asked to move from {unit} into {scope}, and the move had not shown "
                f"after {MOVE_WAIT_S:g}s; stopping {unit} may still stop this run."
            )
        sleep(MOVE_POLL_S)
