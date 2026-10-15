"""Block on a process exiting, for tests that spawn something detached.

A test that backgrounds a process — `deploy.sh --detach`, `spawn_logged`, a grandchild a
timeout must kill — then needs to know when it has gone. A fixed-cap polling loop misses the
cap on a loaded worker. A pidfd is the kernel's own signal for the same event: it becomes
readable the moment the process terminates, before anything reaps it, and it works for a
pid this process did not spawn. `select` on it is a wait with a deadline and no polling.
"""

import errno
import os
import select
from pathlib import Path


def _number_outlived_its_process(pid: int) -> bool:
    """Whether `pid` names no task at all, so EINVAL means its process was already reaped.

    `pidfd_open` raises ESRCH only once the pid NUMBER is free. A session or process-group
    leader's number stays allocated while any member of its group lives, because the
    member still carries it as its PGID/SID; reap the leader first and `pidfd_open` on its
    number raises EINVAL instead (#5093: `spawn_logged` starts `sh` with
    `start_new_session=True`, its reaper thread reaps `sh`, and the `sleep` child is still
    exiting). `/proc/<pid>` is absent in that case. It is present for the other EINVAL a
    positive pid can raise, a live thread that is not its thread group's leader — a wrong
    pid, which must stay an error rather than read as "exited".
    """
    return pid > 0 and not Path(f"/proc/{pid}").exists()


def wait_for_exit(pid: int, timeout: float = 60.0) -> bool:
    """Return True once `pid` has terminated, or False after `timeout` seconds.

    Terminated means exited, including a zombie not yet reaped; a pid that no longer
    exists at all counts as terminated too, since something reaped it already. So does a
    reaped group leader whose number its surviving group still holds. A pid that names a
    non-leader thread, or a non-positive pid, raises the kernel's EINVAL.
    """
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError as err:
        if err.errno == errno.EINVAL and _number_outlived_its_process(pid):
            return True
        raise
    try:
        readable, _, _ = select.select([fd], [], [], timeout)
        return bool(readable)
    finally:
        os.close(fd)
