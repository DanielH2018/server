"""The landing turn served by ticket, with the real lock and the real line in `LAND_TURN_DIR`.

Each waiter runs `turn._wait` in a thread whose sleep blocks until the test lets it poll
again, so the test decides which waiter polls first.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_turn_tickets.py
"""

import os
import threading
from dataclasses import replace
from pathlib import Path

from _land_fakes import Fakes
from deploy_tools.land_lib import line, turn
from deploy_tools.land_lib.outcome import say

HEAD = "a" * 40
_TIMEOUT_S = 10


def _turn_dir() -> Path:
    return Path(os.environ[turn.TURN_DIR_ENV])


def _lock() -> Path:
    return _turn_dir() / turn.LOCK.name


def _landing(landing, pr: str, **tools):
    ln, _ = landing(Fakes())
    ln.opts = replace(ln.opts, pr=pr)
    ln.tools = replace(ln.tools, line=None, try_turn=None, **tools)
    return ln


class _Waiter:
    """One landing's `turn._wait` in a thread that polls once each time the test asks."""

    def __init__(self, ln) -> None:
        self._asleep, self._wake = threading.Semaphore(0), threading.Semaphore(0)
        self.release = None
        ln.tools = replace(ln.tools, sleep=self._sleep)
        place = line.Place(turn.line_of(ln), ln.opts.pr, say)
        self._thread = threading.Thread(target=self._run, args=(ln, place), daemon=True)
        self._thread.start()
        assert self._asleep.acquire(timeout=_TIMEOUT_S)

    def _sleep(self, _seconds: float) -> None:
        self._asleep.release()
        self._wake.acquire()

    def _run(self, ln, place: line.Place) -> None:
        try:
            self.release, _ = turn._wait(ln, place, HEAD)
        finally:
            self._asleep.release()

    def poll(self) -> None:
        """Let the waiter poll once more, and return once it sleeps or its wait ends."""
        self._wake.release()
        assert self._asleep.acquire(timeout=_TIMEOUT_S)


def test_the_older_waiter_takes_a_freed_turn_though_the_younger_polls_first(landing):
    holder = turn.try_take(_lock())
    assert holder is not None
    older = _Waiter(_landing(landing, "7"))
    younger = _Waiter(_landing(landing, "8"))
    holder()
    younger.poll()
    assert younger.release is None
    older.poll()
    assert older.release is not None
    # The older landing left the line as it took the turn, so the younger defers to nobody
    # and waits only for the lock.
    younger.poll()
    assert younger.release is None
    older.release()
    younger.poll()
    assert younger.release is not None
    younger.release()


def test_a_dead_waiters_place_stops_deferring_others_once_it_is_stale(landing):
    line.here(_turn_dir() / line.DIR.name)("join", "7", HEAD, "1")
    dead = _turn_dir() / line.DIR.name / "7"

    def sleep(_seconds: float) -> None:
        os.utime(dead, (0, 0))

    ln = _landing(landing, "8", sleep=sleep)
    release, waited = turn._wait(ln, line.Place(turn.line_of(ln), "8", say), HEAD)
    assert release is not None
    release()
    # It deferred on its first poll, while the dead place was fresh, and took the turn on
    # the next.
    assert waited == ln.opts.merge_poll


def test_a_landing_that_steps_out_of_the_turn_keeps_its_ticket(landing):
    ln = _landing(landing, "7")
    stood = []

    def wait_outside() -> None:
        stood.append((_turn_dir() / line.DIR.name / "7").exists())
        line.here(_turn_dir() / line.DIR.name)("join", "8", HEAD, "")

    with turn.held(ln) as take_turn:
        assert take_turn(HEAD)[0]
        ln.step_out_of_turn(wait_outside, "for a test")
        # A ticket taken anew would be younger than #8's, and the landing would defer to it.
        assert ln.give_back_turn is not None
    # It stood nowhere during the wait outside the turn.
    assert stood == [False]
