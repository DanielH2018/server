"""Who takes the landing turn, and which drafts its holder stacks, over generated landings.

The machine drives `queue_machine.may_take` and `queue_machine.stackable`, the functions
`turn._wait` and `batch._members` call, through arrivals, polls, releases, step-outs and
deaths, and checks the predicates in `core/invariants.py` after every step. It stands in for
the I/O around those functions: one holder slot for the `flock`, a poll every `MERGE_POLL_S`,
and a place that stays in the line for `line.FRESH_S` after its landing dies. Each call
builds its `Queue` the way its caller in `land_lib` does. The seed and the example count come
from `tests/hypothesis_profiles.py`; only the sequence length is set here.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_queue_machine.py
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from deploy_tools.land_lib import batch, line
from deploy_tools.land_lib.core import invariants, queue_machine, state
from deploy_tools.land_lib.options import MERGE_POLL_S

STEPS = settings(stateful_step_count=40)
BATCH_FULL = "a batch held every PR it may"
SERIAL_LEFT = "a holder left a serial draft in the line"
DEAD_STACKED = "a holder stacked a dead landing's fresh place"
STEPPED_OUT = "a holder stepped out of the turn"


@dataclass
class Waiter:
    """One landing that waits for the turn, or holds it.

    Attributes:
        ready_since: When it could last use the turn: its arrival, or the end of a wait
            outside the turn. Later than the machine's clock during that wait, while the
            landing stands nowhere.
        died_at: When its process ended without leaving the line, or None while it lives.
    """

    place: state.Place
    serial: bool
    next_poll: int
    ready_since: int
    died_at: int | None = None


class Landings:
    """The turn, the landings that wait for it, and the batches its holders formed."""

    def __init__(
        self,
        record: Callable[[str], None],
        may_take: Callable[[state.Queue, state.Place], bool],
        stackable: Callable[[state.Queue], Iterator[state.Place]],
    ) -> None:
        self.record, self.may_take, self.stackable = record, may_take, stackable
        self.now = 0
        self.holder: Waiter | None = None
        self.free_since = 0
        self.waiting: dict[str, Waiter] = {}
        self.batches: list[list[str]] = []

    def _fresh(self, w: Waiter) -> bool:
        return w.died_at is None or self.now - w.died_at < line.FRESH_S

    def ready(self) -> list[Waiter]:
        """The live landings that could use the turn now."""
        return [
            w
            for w in self.waiting.values()
            if w.died_at is None and w.ready_since <= self.now
        ]

    def _line(self) -> state.Queue:
        """The queue `batch._members` builds from the fresh places the line lists."""
        places = tuple(
            w.place
            for w in self.waiting.values()
            if w.place.head and w.ready_since <= self.now and self._fresh(w)
        )
        return state.Queue(places, lambda p: self.waiting[p.pr].serial)

    def _grant(self, w: Waiter) -> None:
        del self.waiting[w.place.pr]
        self.holder = w
        members: list[state.Place] = []
        if w.place.head and not w.serial:
            queue = self._line()
            members = list(islice(self.stackable(queue), batch.MAX_STACKED - 1))
            if any(self.waiting[p.pr].serial for p in queue.places):
                self.record(SERIAL_LEFT)
            if any(self.waiting[p.pr].died_at is not None for p in members):
                self.record(DEAD_STACKED)
            for member in members:
                del self.waiting[member.pr]
        if len(members) == batch.MAX_STACKED - 1:
            self.record(BATCH_FULL)
        self.batches.append([w.place.pr, *(p.pr for p in members)])

    def _poll(self, w: Waiter) -> None:
        # As `turn._wait` asks: with a queue of the landing's own place.
        mine = state.Queue((w.place,))
        if self.holder is None and self.may_take(mine, w.place):
            self._grant(w)
        else:
            w.next_poll = self.now + MERGE_POLL_S

    def arrive(self, pr: str, draft: bool, serial: bool) -> None:
        """A landing asks for the turn, and takes it at once if it may."""
        w = Waiter(
            state.Place(pr, f"head-{pr}" if draft else ""), serial, self.now, self.now
        )
        self.waiting[pr] = w
        self._poll(w)

    def release(self) -> None:
        """The holder merges, or ends, and gives the turn up."""
        self.holder, self.free_since = None, self.now

    def step_out(self, wait_s: int) -> None:
        """The holder gives the turn back for a wait of `wait_s`, then asks with its head."""
        w = self.holder
        assert w is not None
        self.holder, self.free_since = None, self.now
        w.ready_since = w.next_poll = self.now + wait_s
        self.waiting[w.place.pr] = w
        self.record(STEPPED_OUT)

    def die(self, pr: str) -> None:
        """A waiting landing's process ends without leaving the line."""
        self.waiting[pr].died_at = self.now

    def tick(self, seconds: int) -> None:
        """Let `seconds` pass, running each poll that falls due, in time order."""
        end = self.now + seconds
        while due := [
            w for w in self.waiting.values() if w.died_at is None and w.next_poll <= end
        ]:
            w = min(due, key=lambda w: (w.next_poll, w.place.pr))
            self.now = max(self.now, w.next_poll)
            self._poll(w)
        self.now = end
        for pr, w in list(self.waiting.items()):
            if not self._fresh(w):
                del self.waiting[pr]


class TurnQueue(RuleBasedStateMachine):
    """Landings that arrive, poll, hold the turn, step out of it and die."""

    may_take = staticmethod(queue_machine.may_take)
    stackable = staticmethod(queue_machine.stackable)

    def __init__(self):
        super().__init__()
        self.landings = Landings(self.record, self.may_take, self.stackable)
        self.seen: set[str] = set()
        self.serial: set[str] = set()

    def record(self, event: str) -> None:
        """A hook for the coverage test."""

    # Four-digit numbers among five-digit ones: the line orders its files by name, not number.
    @rule(
        pr=st.integers(9990, 10030).map(str),
        draft=st.sampled_from([True, True, True, False]),
        serial=st.sampled_from([False, False, False, False, True]),
    )
    def arrive(self, pr, draft, serial):
        if pr not in self.seen:
            self.seen.add(pr)
            if serial:
                self.serial.add(pr)
            self.landings.arrive(pr, draft, serial)

    @rule(first=st.sampled_from(range(10500, 10700, batch.MAX_STACKED)))
    def a_fan_out_arrives(self, first):
        """As many drafts as one batch holds, started together."""
        for pr in range(first, first + batch.MAX_STACKED):
            self.arrive(str(pr), True, False)

    @rule(seconds=st.sampled_from([7, 30, 45, 300]))
    def tick(self, seconds):
        self.landings.tick(seconds)

    @precondition(lambda self: self.landings.holder is not None)
    @rule()
    def release(self):
        self.landings.release()

    @precondition(lambda self: self.landings.holder is not None)
    @rule(wait_s=st.sampled_from([60, 600]))
    def step_out(self, wait_s):
        self.landings.step_out(wait_s)

    @precondition(lambda self: self.landings.ready())
    @rule(which=st.integers(0, 40))
    def die(self, which):
        live = sorted(w.place.pr for w in self.landings.ready())
        self.landings.die(live[which % len(live)])

    @invariant()
    def a_free_turn_goes_to_a_waiting_landing_within_one_poll(self):
        if self.landings.holder is None:
            assert invariants.free_turn_is_taken(
                self.landings.free_since,
                self.landings.now,
                [w.ready_since for w in self.landings.ready()],
                MERGE_POLL_S,
            )

    @invariant()
    def a_batch_holds_no_serial_pr(self):
        for stacked in self.landings.batches[-1:]:
            assert invariants.serial_lands_alone(stacked, self.serial)


TestTurnQueue = TurnQueue.TestCase
TestTurnQueue.settings = STEPS


def _ignores_lands_alone(queue: state.Queue) -> Iterator[state.Place]:
    return iter(sorted((p for p in queue.places if p.head), key=lambda p: p.pr))


class StacksASerialPr(TurnQueue):
    stackable = staticmethod(_ignores_lands_alone)


class NeverTakesTheTurn(TurnQueue):
    may_take = staticmethod(lambda _queue, _me: False)


@pytest.mark.parametrize("machine", [StacksASerialPr, NeverTakesTheTurn])
def test_the_machine_fails_a_rule_that_breaks_an_invariant(machine):
    with pytest.raises(AssertionError):
        run_state_machine_as_test(machine, settings=STEPS)


def test_the_generated_queues_reach_every_event():
    """Non-vacuity: a generator that never filled a batch would pass every invariant."""
    seen: set[str] = set()

    class Recording(TurnQueue):
        def record(self, event: str) -> None:
            seen.add(event)

    run_state_machine_as_test(Recording, settings=STEPS)
    assert seen == {BATCH_FULL, SERIAL_LEFT, DEAD_STACKED, STEPPED_OUT}


def test_a_holder_stacks_in_the_order_the_line_lists_its_places(tmp_path):
    places = line.here(tmp_path / "line")
    for pr in ("10465", "9998", "10458"):
        places("join", pr, f"head-{pr}")
    listed = [
        state.Place(*row.split())
        for row in places("waiting", str(line.FRESH_S)).splitlines()
    ]
    queue = state.Queue(tuple(reversed(listed)))
    assert list(queue_machine.stackable(queue)) == listed
    assert [p.pr for p in listed] == ["10458", "10465", "9998"]


def test_stackable_asks_lands_alone_of_no_place_past_what_its_caller_takes():
    asked: list[str] = []

    def lands_alone(place: state.Place) -> bool:
        asked.append(place.pr)
        return place.pr == "2"

    places = tuple(state.Place(pr, f"head-{pr}") for pr in "54321")
    stacked = islice(queue_machine.stackable(state.Queue(places, lands_alone)), 2)
    assert [p.pr for p in stacked] == ["1", "3"]
    assert asked == ["1", "2", "3"]


def test_stackable_leaves_out_a_place_with_no_head():
    queue = state.Queue((state.Place("1"), state.Place("2", "head-2")))
    assert [p.pr for p in queue_machine.stackable(queue)] == ["2"]


@pytest.mark.parametrize(
    ("stacked", "holds"),
    [
        pytest.param(["1", "2"], True, id="no-serial-pr-is_clean"),
        pytest.param(["9"], True, id="a-serial-pr-alone-is_clean"),
        pytest.param(["1", "9"], False, id="a-serial-member-is_flagged"),
        pytest.param(["9", "1"], False, id="a-serial-holder-with-members-is_flagged"),
    ],
)
def test_serial_lands_alone(stacked, holds):
    assert invariants.serial_lands_alone(stacked, {"9"}) is holds


@pytest.mark.parametrize(
    ("now", "holds"),
    [
        pytest.param(130, True, id="taken-within-the-bound-is_clean"),
        pytest.param(131, False, id="free-past-the-bound-is_flagged"),
    ],
)
def test_free_turn_is_taken(now, holds):
    # Freed at 100, by a landing ready since 50: the bound runs from the release.
    assert invariants.free_turn_is_taken(100, now, [50], 30) is holds
