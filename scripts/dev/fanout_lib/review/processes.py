"""The review pipeline's process boundary, and the reap that ends a red phase.

`run_process` is the one call every `claude`, `git` and `gh` invocation goes through.

`reaping` kills whatever the red phase left running (#3852). The red author runs with
`--permission-mode auto`, so it could start
`nohup sh -c 'sleep 120; echo ... > .claude/settings.local.json' &` and have it write after
the worktree is reset. While the block is open the pipeline is a child subreaper, so an
orphaned descendant reparents to it rather than to init, `setsid` or not, and the block's exit
kills each one. The red author's tests can do the same when a later gate runs them, so the
green gate, the hunk check and the base check are reaped as well (#4508).

DECIDED: the red phase and every test run the pipeline starts are reaped, and nothing else.
The landing runs `land.sh --detach`, which double-forks so the landing outlives the call
(`land_lib/detach.py`). Under a subreaper the landing would reparent to the pipeline and be
killed. The implement and fix phases run the red tests as well, and are not reaped: the
implementer may leave a process on purpose, and #3865 covers what a session starts.

A process started outside this tree, such as through `systemd-run --user`, is not reached.
"""

import ctypes
import os
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36


def run_process(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, check=False
    )


def _set_subreaper(on: bool) -> None:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0)


def _children() -> list[int]:
    me = str(os.getpid())
    found = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            # The command name can hold spaces and parentheses; the ppid follows the last ")".
            fields = stat.read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if fields and fields[1] == me:
            found.append(int(stat.parent.name))
    return found


def _reap_descendants() -> None:
    """SIGKILL and reap every child of this process, and every orphan that reparents to it."""
    for _ in range(100):
        children = _children()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in children:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


@contextmanager
def reaping() -> Generator[None]:
    """Kill every process started inside the block once it exits, orphans included.

    The caller must start no process inside the block that should outlive it. On exit this
    process is a subreaper no longer.
    """
    _set_subreaper(True)
    try:
        yield
    finally:
        _reap_descendants()
        _set_subreaper(False)
