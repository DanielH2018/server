"""The landing turn: one landing at a time from a PR's CI run to its merge.

A PR's CI run tests the tree of master with the PR merged in. Master's own run after the merge
skips its jobs only when the merge made that same tree (`scripts/dev/tested_tree.py`), so
master must stay where the PR's run found it. A landing that holds the turn starts its PR's
one run on the master of that moment (`merge._start_run`), waits for it and merges, and no
other landing that takes turns merges in between. `docs/ci-throughput.md` has the
measurements: a merge cost 1.4 pull-request runs and 0.7 full master runs before the turn,
against one run with it.

The turn is an advisory lock on one file, held by the landing's own process, so a landing that
dies gives it up. It is not a safety gate. A landing that cannot take it within `merge_timeout`
goes on without it and merges as a landing without the turn does. A landing on another host,
and a landing without `LAND_QUEUE`, take no turn and can still move master.

`LAND_QUEUE` turns it on, for a landing that merges its PR directly.
"""

import contextlib
import fcntl
import os
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


def try_take(path: _Path = LOCK) -> Callable[[], None] | None:
    """Take the turn without waiting.

    The lock needs only a read-only descriptor, so a landing run by one user can lock a file
    that another user's landing created.

    Returns:
        The call that gives the turn up, or None while another landing holds it.

    Raises:
        OSError: The lock file can be neither opened nor created.
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
    return lambda: os.close(fd)


def _wait(ln: Landing) -> Callable[[], None] | None:
    """Poll for the turn for up to `merge_timeout`; None when the landing goes on without it."""
    o = ln.opts
    try_turn = ln.tools.try_turn or try_take
    waiting = OnChange(say)
    waited = 0
    while True:
        try:
            release = try_turn()
        except OSError as exc:
            say(
                f"could not open the landing turn's lock ({exc}); going on without a turn"
            )
            return None
        if release:
            say(f"took the landing turn after {waited}s")
            return release
        if waited >= o.merge_timeout:
            say(
                f"another landing has held the turn for {waited}s, reaching "
                f"merge_timeout={o.merge_timeout}s; going on without a turn"
            )
            return None
        waiting(
            "waiting for the landing turn: another landing is between its CI run and its "
            "merge (`probe.py landing` lists the landings in flight)"
        )
        ln.tools.sleep(o.merge_poll)
        waited += o.merge_poll


@contextlib.contextmanager
def held(ln: Landing) -> Generator[bool]:
    """Hold the landing turn for the merge wait, and give it up however that wait ends.

    Yields:
        Whether this landing holds the turn. False for a landing without `LAND_QUEUE`, for one
        that merges nothing itself, and for one that waited `merge_timeout` without getting it.
    """
    if not (ln.opts.queue and ln.direct_merge_subject):
        yield False
        return
    release = _wait(ln)
    try:
        yield release is not None
    finally:
        if release:
            release()
