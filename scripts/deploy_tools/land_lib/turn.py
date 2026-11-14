"""The landing turn: one landing at a time from a PR's CI run to its merge.

A PR's CI run tests the tree of master with the PR merged in. Master's own run after the merge
skips its jobs only when the merge made that same tree (`scripts/dev/tested_tree.py`), so
master must stay where the PR's run found it. A landing that holds the turn starts its PR's
one run on the master of that moment (`merge._start_run`), waits for it and merges, and no
other landing that takes turns merges in between. `docs/ci-throughput.md` has the
measurements: a merge cost 1.4 pull-request runs and 0.7 full master runs before the turn,
against one run with it.

The turn is an advisory lock on one file on `TURN_HOST`. A landing there locks the file itself.
A landing on another host holds the same lock through an ssh session that lasts as long as
the landing, so both hosts take turns with each other. Either way the lock ends with the
landing's process, so a landing that dies gives the turn up.

A landing asks for the turn when it is ready to use it (`merge.await_merge`): before the one
run of a head that has had none, and otherwise once its head is green. It never holds the
turn while a run that started before it finishes.

The turn is not a safety gate. A landing that cannot take it within `merge_timeout`, or
cannot reach `TURN_HOST`, goes on without it and merges as a landing without the turn does.

Every landing that merges its PR directly takes the turn. `LAND_QUEUE=0` turns it off, and a
landing run that way can still move master under the holder.
"""

import contextlib
import fcntl
import inspect
import os
import shlex
import subprocess
from collections.abc import Callable, Generator

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say
from lib.progress import OnChange

# /run/lock is a tmpfs every user can create a file in, and one file there serves every
# landing on the host, whichever user runs it.
LOCK = _Path("/run/lock/homelab-land-turn.lock")


# The host whose file is the turn. The lander units run there.
TURN_HOST = "daniel-box"
# How long a landing on another host waits for `TURN_HOST` to give up the lock it released.
_SSH_EXIT_S = 10


def _lock(path: str) -> int | None:
    """Open the turn's file and lock it without waiting.

    The lock needs only a read-only descriptor, so a landing run by one user can lock a file
    that another user's landing created. It names nothing but `os` and `fcntl`, because
    `_HOLD` runs this source on `TURN_HOST`.

    Returns:
        The locked descriptor, or None while another landing holds the lock.

    Raises:
        OSError: The file can be neither opened nor created.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.fchmod(fd, 0o644)  # a umask of 077 would lock every other user out
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError):
            return None
        raise
    return fd


# What a landing on another host runs on `TURN_HOST`: take the lock, say which way it went,
# and hold it until stdin closes, which is when the landing's ssh session ends.
_HOLD = f"""import fcntl, os, sys
{inspect.getsource(_lock)}
fd = _lock(sys.argv[1])
print("busy" if fd is None else "held", flush=True)
if fd is not None:
    sys.stdin.read()
"""


def try_take(path: _Path = LOCK) -> Callable[[], None] | None:
    """Take the turn on this host without waiting.

    Returns:
        The call that gives the turn up, or None while another landing holds it.

    Raises:
        OSError: The lock file can be neither opened nor created.
    """
    fd = _lock(str(path))
    return None if fd is None else lambda: os.close(fd)


def try_take_over_ssh(
    path: _Path = LOCK, host: str = TURN_HOST
) -> Callable[[], None] | None:
    """Take the turn on `host` without waiting, through an ssh session that holds it.

    Returns:
        The call that gives the turn up, or None while another landing holds it.

    Raises:
        OSError: `host` did not answer, or could not open the lock file.
    """
    remote = f"python3 -c {shlex.quote(_HOLD)} {shlex.quote(str(path))}"
    session = subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            host,
            remote,
        ],  # fmt: skip
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdin, stdout, stderr = session.stdin, session.stdout, session.stderr
    assert stdin and stdout and stderr

    def release() -> None:
        stdin.close()
        try:
            session.wait(_SSH_EXIT_S)
        except subprocess.TimeoutExpired:
            session.kill()
            session.wait()
        stdout.close()
        stderr.close()

    answer = stdout.readline().strip()
    if answer == "held":
        return release
    why = stderr.read().strip().splitlines()
    release()
    if answer == "busy":
        return None
    raise OSError(f"ssh {host}: {why[-1] if why else 'no answer'}")


def _taker(hostname: str) -> Callable[[], Callable[[], None] | None]:
    """The call that takes the turn from the host named `hostname`, qualified or not."""
    here = hostname.split(".")[0] == TURN_HOST
    return try_take if here else try_take_over_ssh


def _wait(ln: Landing) -> tuple[Callable[[], None] | None, int]:
    """Poll for the turn for up to `merge_timeout`.

    Returns:
        The call that gives the turn up, or None when the landing goes on without it, and the
        seconds the wait took.
    """
    o = ln.opts
    try_turn = ln.tools.try_turn or _taker(ln.tools.hostname())
    waiting = OnChange(say)
    waited = 0
    while True:
        try:
            release = try_turn()
        except OSError as exc:
            say(
                f"could not open the landing turn's lock ({exc}); going on without a turn"
            )
            return None, waited
        if release:
            say(f"took the landing turn after {waited}s")
            return release, waited
        if waited >= o.merge_timeout:
            say(
                f"another landing has held the turn for {waited}s, reaching "
                f"merge_timeout={o.merge_timeout}s; going on without a turn"
            )
            return None, waited
        waiting(
            "waiting for the landing turn: another landing is between its CI run and its "
            "merge (`probe.py landing` lists the landings in flight)"
        )
        ln.tools.sleep(o.merge_poll)
        waited += o.merge_poll


@contextlib.contextmanager
def held(ln: Landing) -> Generator[Callable[[], tuple[bool, int]]]:
    """Offer the landing turn to the merge wait, and give it up however that wait ends.

    Yields:
        The call that takes the turn. It waits in line for up to `merge_timeout` and returns
        whether this landing holds the turn and the seconds that call waited. It asks once:
        a later call answers at once with what the first one got. It never asks for a landing
        with `LAND_QUEUE=0` or for one that merges nothing itself.
    """
    release: Callable[[], None] | None = None
    asked = not (ln.opts.queue and ln.direct_merge_subject)

    def take() -> tuple[bool, int]:
        nonlocal release, asked
        if asked:
            return release is not None, 0
        asked = True
        release, waited = _wait(ln)
        return release is not None, waited

    try:
        yield take
    finally:
        if release:
            release()
