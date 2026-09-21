"""Block on a process exiting, for tests that spawn something detached.

A test that backgrounds a process — `deploy.sh --detach`, `spawn_logged`, a grandchild a
timeout must kill — then needs to know when it has gone. Polling `os.kill(pid, 0)` in a
`time.sleep(0.05)` loop with a two-second cap was the shape until #2159, and a loaded
worker missed the cap. A pidfd is the kernel's own signal for the same event: it becomes
readable the moment the process terminates, before anything reaps it, and it works for a
pid this process did not spawn. `select` on it is a wait with a deadline and no polling.
"""

import os
import select


def wait_for_exit(pid: int, timeout: float = 60.0) -> bool:
    """Return True once `pid` has terminated, or False after `timeout` seconds.

    Terminated means exited, including a zombie not yet reaped; a pid that no longer
    exists at all counts as terminated too, since something reaped it already.
    """
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        readable, _, _ = select.select([fd], [], [], timeout)
        return bool(readable)
    finally:
        os.close(fd)
