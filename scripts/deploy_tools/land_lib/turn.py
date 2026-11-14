"""The landing turn: one landing at a time from a PR's CI run to its merge.

A PR's CI run tests the tree of master with the PR merged in. Master's own run after the merge
skips its jobs only when the merge made that same tree (`scripts/dev/tested_tree.py`), so
master must stay where the PR's run found it. A landing that holds the turn marks its draft PR
ready, waits for that one run and merges, and no other landing that takes turns merges in
between. `docs/ci-throughput.md` has the measurements: a merge cost 1.4 pull-request runs and
0.7 full master runs before the turn, against one run with it.

The turn is an advisory lock on one file, held by the landing's own process, so a landing that
dies gives it up. It is not a safety gate. A landing that cannot take it within `merge_timeout`
goes on without it and merges as a landing without the turn does. A landing on another host,
and a landing without `LAND_QUEUE`, take no turn and can still move master.

`LAND_QUEUE` turns it on, for a landing that merges its PR directly.
"""

import contextlib
import fcntl
import os
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
# How often, and how far apart in seconds, `_mark_ready` reads `mergeable` while GitHub still
# answers UNKNOWN.
MERGEABLE_READS = 6
MERGEABLE_POLL_S = 5


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


def _mark_ready(ln: Landing) -> None:
    """Mark a draft PR ready, which starts the CI run the merge waits for.

    That run tests the merge of the PR into master as GitHub last built it, and GitHub builds
    it again only when something asks whether the PR merges. The read of `mergeable` asks, so
    it comes first, and the run tests the PR on the master this landing merges into.
    """
    if ln.view("isDraft").get("isDraft") is not True:
        return
    pr = str(ln.opts.pr)
    for _ in range(MERGEABLE_READS):
        if ln.view("mergeable").get("mergeable") != "UNKNOWN":
            break
        ln.tools.sleep(MERGEABLE_POLL_S)
    try:
        ln.tools.gh("pr", "ready", pr)
    except subprocess.CalledProcessError as exc:
        why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
        ln.die(
            f"PR #{pr} is a draft and could not be marked ready ({why}); run "
            f"`gh pr ready {pr}`, then re-run this",
            1,
        )
    except subprocess.TimeoutExpired:
        ln.die(
            f"PR #{pr} is a draft and marking it ready timed out; run `gh pr ready {pr}`, "
            "then re-run this",
            1,
        )
    say(f"PR #{pr} was a draft; marked it ready, which starts its CI run")


@contextlib.contextmanager
def held(ln: Landing) -> Generator[bool]:
    """Hold the landing turn for the merge wait, and give it up however that wait ends.

    Inside the turn it marks a draft PR ready, so the PR's one CI run starts after every
    earlier landing's merge. The draft is marked ready even when the turn never came, because
    a draft cannot merge.

    Yields:
        Whether this landing holds the turn. False for a landing without `LAND_QUEUE`, for one
        that merges nothing itself, and for one that waited `merge_timeout` without getting it.
    """
    if not (ln.opts.queue and ln.direct_merge_subject):
        yield False
        return
    release = _wait(ln)
    try:
        _mark_ready(ln)
        yield release is not None
    finally:
        if release:
            release()
