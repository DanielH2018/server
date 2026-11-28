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
landing's process, so a landing that dies gives the turn up. sshd on `TURN_HOST` never probes
a client, so a host that loses power or its network sends no close. A landing on another host
therefore writes a heartbeat into the session every `HEARTBEAT_S`, and the script on
`TURN_HOST` frees the lock once three intervals pass without one.

A landing asks for the turn when it is ready to use it (`merge.await_merge`): before the one
run of a head that has had none, and otherwise once its head is green. It never holds the
turn while a run that started before it finishes, nor through a rate-limit wait: it gives the
turn back for the wait's length and asks for it again before it goes on
(`Landing.without_turn`).

A landing that waits with a draft stands in the line beside the lock (`line.py`), so the
landing that holds the turn can test that draft's head with its own. A landing whose place
reads `merged` stops waiting: the holder merged its PR. One whose place reads `alone` leaves
the line and waits on for the turn: a batch that tested its PR was not green and the holder's
own run was not red, and it forms no batch of its own (`batch.py`). One whose place reads
`split` waits on in the line until the holder's own run settles it, and lands alone if it
takes the turn first.

The turn is not a safety gate. A landing that cannot take it within `merge_timeout`, or
cannot reach `TURN_HOST`, goes on without it and merges as a landing without the turn does.

Every landing that merges its PR directly takes the turn. A merge made outside `land.sh` can
still move master under the holder.

A holder whose landing code lags master forms no batch (`batch._members`). When it holds the
turn as a lock on this host, it hands the turn to a landing of the same PR from a fresh checkout
of origin/master instead (`HandOver`). The new landing inherits the locked descriptor through
`fresh.TURN_FD_ENV`, so no other landing takes the turn in between. From then on the turn
ends with the new landing, not with the one that handed it over. A landing that took a turn
over never hands it on, and a turn held over ssh is never handed over.

`LAND_TURN_DIR` moves the lock and the line into a directory on this host, whatever the host,
so a test that runs a whole landing never takes the real turn.
"""

import contextlib
import fcntl
import inspect
import os
import shlex
import subprocess
import threading
from collections.abc import Callable, Generator, Sequence
from typing import IO, NoReturn

from pathlib import Path as _Path

from deploy_tools.land_lib import line
from deploy_tools.land_lib.fresh import TURN_FD_ENV
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Verdict, say
from deploy_tools.land_lib.serial_lock import (
    LOCK_DIR,
    TURN_DIR_ENV,
    TURN_HOST,
    is_turn_host,
)
from lib.progress import OnChange

LOCK = LOCK_DIR / "homelab-land-turn.lock"


# How long a landing on another host waits for `TURN_HOST` to give up the lock it released.
_SSH_EXIT_S = 10
# DECIDED: 5.0 s, three missed heartbeats free an ssh-held lock (operator, 2026-11-28). A landing
# on another host that stays alive but silent that long (a stopped process, or a network stall
# between the hosts) loses its lock unaware, so a second landing can take the turn. The turn is
# not a safety gate: the merge keeps its head-SHA pin and its stale_base check.
HEARTBEAT_S = 5.0


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


def lock_below(paths: list[str], cap: int) -> int | None:
    """Lock one free file of `paths` while other landings hold fewer than `cap` of them.

    It locks every free file before it counts, so no landing can take one in between, then
    keeps the first and closes the rest. It names nothing but `os` and `_lock`, because
    `_HOLD` runs this source on `TURN_HOST`.

    Returns:
        The locked descriptor, or None while other landings hold `cap` or more of the files.

    Raises:
        OSError: A file can be neither opened nor created.
    """
    fds = []
    try:
        for path in paths:
            fd = _lock(path)
            if fd is not None:
                fds.append(fd)
    except OSError:
        for fd in fds:
            os.close(fd)
        raise
    keep = fds[0] if fds and len(paths) - len(fds) < cap else None
    for fd in fds:
        if fd != keep:
            os.close(fd)
    return keep


# argv: the heartbeat interval in seconds, the cap, then the lock files.
_HOLD = f"""import fcntl, os, select, sys
{inspect.getsource(_lock)}
{inspect.getsource(lock_below)}
fd = lock_below(sys.argv[3:], int(sys.argv[2]))
print("busy" if fd is None else "held", flush=True)
if fd is not None:
    while select.select([0], [], [], 3 * float(sys.argv[1]))[0] and os.read(0, 4096):
        pass
"""


class LocalTurn:
    """The turn held as a lock on this host's file; calling it gives the turn up.

    Attributes:
        fd: The locked descriptor. A child process that inherits it holds the same lock.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __call__(self) -> None:
        # Unlocks before closing: the landing that handed this turn over keeps its copy open.
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


class HandOver(Exception):
    """A turn this landing passes to a landing of its PR from a fresh checkout.

    Attributes:
        fd: The turn's locked descriptor, for the new landing to inherit.
    """

    def __init__(self, fd: int, why: str) -> None:
        super().__init__(why)
        self.fd = fd


def try_take(path: _Path = LOCK) -> LocalTurn | None:
    """Take the turn on this host without waiting.

    Returns:
        The call that gives the turn up, or None while another landing holds it.

    Raises:
        OSError: The lock file can be neither opened nor created.
    """
    fd = _lock(str(path))
    return None if fd is None else LocalTurn(fd)


def _adopt() -> LocalTurn | None:
    """The turn a landing handed to this one (`HandOver`), or None when none was."""
    fd = os.environ.pop(TURN_FD_ENV, "")
    if not fd:
        return None
    try:
        fcntl.flock(int(fd), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError) as exc:
        say(f"could not take over the landing turn handed to this landing ({exc})")
        return None
    say("took over the landing turn from the landing that handed it to this checkout")
    return LocalTurn(int(fd))


def hold_over_ssh(
    host: str, script: str, args: Sequence[str]
) -> tuple[str, subprocess.Popen[str], Callable[[], None]]:
    """Run `script` on `host` with `args`, through an ssh session it can outlast a call in.

    The script prints one line, then reads its stdin, which is how a lock it took lasts as
    long as the session.

    Returns:
        The line the script printed, the session, and the call that ends the session.

    Raises:
        OSError: `host` did not answer, or the script printed nothing.
    """
    remote = shlex.join(["python3", "-c", script, *args])
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
    if not answer:
        why = stderr.read().strip().splitlines()
        release()
        raise OSError(f"ssh {host}: {why[-1] if why else 'no answer'}")
    return answer, session, release


def start_heartbeat(stdin: IO[str], beat: float) -> Callable[[], None]:
    """Write a blank line into `stdin` at once and every `beat` seconds after, from a thread.

    The thread ends when the pipe breaks or the call this returns is made. A process forked
    from this one inherits no thread, so it keeps a lock held over ssh only by starting its
    own heartbeat on the descriptor it inherits.

    Returns:
        The call that stops the heartbeat. It returns once the thread has ended, so `stdin`
        can be closed after it, and a fork made after it copies no thread.
    """
    stop = threading.Event()

    def beats() -> None:
        while True:
            try:
                # Unbuffered: a line left in the buffer by a broken pipe would raise again
                # from `stdin.close()`.
                os.write(stdin.fileno(), b"\n")
            except OSError, ValueError:
                return
            if stop.wait(beat):
                return

    thread = threading.Thread(target=beats, daemon=True)
    thread.start()

    def end() -> None:
        stop.set()
        thread.join()

    return end


def try_take_over_ssh(
    paths: Sequence[_Path] = (LOCK,),
    cap: int = 1,
    host: str = TURN_HOST,
    beat: float = HEARTBEAT_S,
) -> Callable[[], None] | None:
    """`lock_below(paths, cap)` on `host` without waiting, through an ssh session.

    The session holds the lock until the call it returns closes it, or until `host` reads no
    heartbeat for three `beat` intervals. A thread of this process writes the heartbeat, so a
    process forked from this one does not keep the lock. The turn is one file under a cap of
    1; the landing slots (`slots.py`) are several.

    Args:
        beat: the seconds between heartbeats; a test shortens it.

    Returns:
        The call that gives the lock up, or None while other landings hold `cap` of the files.

    Raises:
        OSError: `host` did not answer, or could not open a lock file.
    """
    answer, session, end = hold_over_ssh(
        host, _HOLD, [str(beat), str(cap), *map(str, paths)]
    )
    if answer == "held":
        assert session.stdin
        stop = start_heartbeat(session.stdin, beat)

        def release() -> None:
            stop()
            end()

        return release
    end()
    if answer == "busy":
        return None
    raise OSError(f"ssh {host}: answered {answer!r}")


def _taker(hostname: str) -> Callable[[], Callable[[], None] | None]:
    """The call that takes the turn from the host named `hostname`."""
    moved = os.environ.get(TURN_DIR_ENV)
    if moved:
        return lambda: try_take(_Path(moved) / LOCK.name)
    return try_take if is_turn_host(hostname) else try_take_over_ssh


def _line(hostname: str, directory: _Path = line.DIR) -> line.Line:
    """The line for the turn, as the host named `hostname` reaches it."""
    moved = os.environ.get(TURN_DIR_ENV)
    if moved:
        return line.here(_Path(moved) / directory.name)
    if is_turn_host(hostname):
        return line.here(directory)
    return line.over_ssh(TURN_HOST, directory)


def line_of(ln: Landing) -> line.Line:
    """The line for the turn, as `ln`'s landing reaches it."""
    return ln.tools.line or _line(ln.tools.hostname())


def _conflicts(ln: Landing) -> NoReturn:
    ln.die(
        f"the landing that holds the turn found PR #{ln.opts.pr}'s head conflicts with "
        "master — rebase it, push, then re-run this",
        1,
        Verdict.MERGE_CONFLICT,
    )


def _lands_alone(ln: Landing) -> None:
    say(f"a batch that tested PR #{ln.opts.pr} was not green; it lands alone")
    ln.lands_alone = True


def _wait(ln: Landing, draft: str) -> tuple[Callable[[], None] | None, int]:
    """Poll for the turn for up to `merge_timeout`, standing in the line with `draft`.

    Each poll adds its seconds to `ln.ledger.turn_wait` as it passes, so a landing that ends
    during the wait, or after it, still records the wait.

    Args:
        draft: The head of a draft that has had no CI run, or "" for any other head.

    Returns:
        The call that gives the turn up, or None when the landing goes on without it, and the
        seconds the wait took.
    """
    o = ln.opts
    try_turn = ln.tools.try_turn or _taker(ln.tools.hostname())
    place = line.Place(line_of(ln), o.pr, draft, say)
    waiting = OnChange(say)
    waited = 0
    before = ln.ledger.turn_wait = ln.ledger.turn_wait or 0
    try:
        while True:
            try:
                release = try_turn()
            except OSError as exc:
                say(
                    f"could not open the landing turn's lock ({exc}); going on without a "
                    "turn"
                )
                return None, waited
            if release:
                # The holder may have answered after this landing's last poll, and frees the turn
                # just after writing `merged`.
                answer = place.answer()
                if answer == line.MERGED:
                    release()
                    say(
                        f"the landing that holds the turn merged PR #{o.pr} with its own"
                    )
                    return None, waited
                if answer == line.CONFLICT:
                    release()
                    _conflicts(ln)
                # `split`: the batch's holder stepped out of the turn before its own run decided.
                if answer in (line.ALONE, line.SPLIT):
                    _lands_alone(ln)
                say(f"took the landing turn after {waited}s")
                return release, waited
            if waited >= o.merge_timeout:
                say(
                    f"waited {waited}s for the landing turn, reaching "
                    f"merge_timeout={o.merge_timeout}s; going on without a turn"
                )
                return None, waited
            answer = place.stand()
            if answer == line.MERGED:
                say(f"the landing that holds the turn merged PR #{o.pr} with its own")
                return None, waited
            if answer == line.CONFLICT:
                _conflicts(ln)
            if answer == line.ALONE:
                _lands_alone(ln)
                place.leave()
                place = line.Place(None, o.pr, "", say)
            waiting(
                "waiting for the landing turn: another landing is between its CI run and "
                "its merge (`probe.py landing` lists the landings in flight)"
            )
            ln.tools.sleep(o.merge_poll)
            waited += o.merge_poll
            ln.ledger.turn_wait = before + waited
    finally:
        place.leave()


@contextlib.contextmanager
def held(ln: Landing) -> Generator[Callable[[str], tuple[bool, int]]]:
    """Offer the landing turn to the merge wait, and give it up however that wait ends.

    Yields:
        The call that takes the turn. Its argument is the head of a draft that has had no CI
        run, which stands in the line during the wait, or "" for any other head. It waits for
        up to `merge_timeout` and returns whether this landing holds the turn and the seconds
        that call waited. It asks once: a later call answers at once with what the first one
        got, until `ln.give_back_turn` gives the turn up and the next call asks again.
        `ln.give_back_turn` and `ln.step_out_of_turn` are set only while this landing holds
        the turn.

    Raises:
        HandOver: from `ln.hand_over_turn`, which leaves the turn locked for the caller.
    """
    release: Callable[[], None] | None = _adopt()
    asked = adopted = release is not None

    def offer() -> None:
        ln.give_back_turn, ln.step_out_of_turn = (
            (give_back, step_out) if release else (None, None)
        )

    def take(draft: str) -> tuple[bool, int]:
        nonlocal release, asked
        if asked:
            return release is not None, 0
        asked = True
        release, waited = _wait(ln, draft)
        offer()
        return release is not None, waited

    def give_back() -> None:
        nonlocal release, asked
        if release:
            release()
        release, asked = None, False
        offer()

    def step_out[T](wait: Callable[[], T], reason: str) -> T:
        give_back()
        say(f"gave the landing turn back {reason}")
        got = wait()
        take("")
        ln.turn_interrupted = True
        return got

    def hand_over(why: str) -> None:
        if adopted or ln.opts.policy_active or not isinstance(release, LocalTurn):
            return
        raise HandOver(release.fd, why)

    offer()
    ln.hand_over_turn = hand_over
    try:
        yield take
    except HandOver:
        release = None
        raise
    finally:
        ln.give_back_turn = ln.step_out_of_turn = ln.hand_over_turn = None
        if release:
            release()
