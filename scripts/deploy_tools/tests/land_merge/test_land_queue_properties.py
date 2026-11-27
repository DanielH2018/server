"""Who takes the landing turn next, over generated arrivals, polls, step-outs and deaths.

The machine drives `_land_queue_model.Queue` under one policy. Under the race, which is what
the code does, a free turn goes to a waiting landing within one poll, and that is all the race
promises. With tickets, no landing that can use the turn sees a later arrival take it, and a
dead landing's ticket holds the turn free for `line.FRESH_S` at most. The seed and the example
count come from `tests/hypothesis_profiles.py`; only the sequence length is set here.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_queue_properties.py
"""

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

from _land_queue_model import (
    BATCH_FULL,
    DEFERRED_TO_DEAD,
    OVERTAKEN,
    STEPPED_OUT,
    Policy,
    Queue,
)
from deploy_tools.land_lib import batch, line

STEPS = settings(stateful_step_count=40)


class TurnQueue(RuleBasedStateMachine):
    """Landings that arrive, poll, hold the turn, step out of it and die."""

    policy: Policy
    no_overtaking = False

    def __init__(self):
        super().__init__()
        self.queue = Queue(self.policy, self.record)
        self.seen: set[int] = set()
        self.serial: set[int] = set()

    def record(self, event: str) -> None:
        """A hook for the coverage test."""

    @rule(
        pr=st.integers(10400, 10440),
        draft=st.sampled_from([True, True, True, False]),
        serial=st.sampled_from([False, False, False, False, True]),
    )
    def arrive(self, pr, draft, serial):
        if pr not in self.seen:
            self.seen.add(pr)
            if serial:
                self.serial.add(pr)
            self.queue.arrive(pr, draft, serial)

    @rule(first=st.sampled_from(range(10500, 10700, batch.MAX_STACKED)))
    def a_fan_out_arrives(self, first):
        """As many drafts as one batch holds, started together."""
        for pr in range(first, first + batch.MAX_STACKED):
            self.arrive(pr, True, False)

    @rule(seconds=st.sampled_from([7, 30, 45, 300]))
    def tick(self, seconds):
        self.queue.tick(seconds)

    @precondition(lambda self: self.queue.holder is not None)
    @rule()
    def release(self):
        self.queue.release()

    @precondition(lambda self: self.queue.holder is not None)
    @rule(wait_s=st.sampled_from([60, 600]))
    def step_out(self, wait_s):
        self.queue.step_out(wait_s)

    @precondition(lambda self: self.queue.waiting)
    @rule(which=st.integers(0, 40))
    def die(self, which):
        live = sorted(pr for pr, w in self.queue.waiting.items() if w.died_at is None)
        if live:
            self.queue.die(live[which % len(live)])

    @invariant()
    def a_free_turn_goes_to_a_waiting_landing_within_the_bound(self):
        assert not self.queue.stuck()

    @invariant()
    def a_batch_holds_no_serial_pr_and_no_more_than_it_may(self):
        for stacked in self.queue.batches[-1:]:
            assert len(stacked) <= batch.MAX_STACKED
            assert not self.serial & set(stacked[1:])
            assert len(stacked) == 1 or stacked[0] not in self.serial

    @invariant()
    def no_landing_sees_a_later_arrival_take_the_turn(self):
        if self.no_overtaking:
            overtaken = {
                pr: w.overtaken for pr, w in self.queue.waiting.items() if w.overtaken
            }
            assert not overtaken


class Race(TurnQueue):
    policy = Policy.RACE


class Tickets(TurnQueue):
    policy = Policy.TICKETS
    no_overtaking = True


TestRace = Race.TestCase
TestRace.settings = STEPS
TestTickets = Tickets.TestCase
TestTickets.settings = STEPS


def test_hypothesis_finds_a_landing_the_race_overtakes():
    class RaceWithNoOvertaking(Race):
        no_overtaking = True

    with pytest.raises(AssertionError):
        run_state_machine_as_test(RaceWithNoOvertaking, settings=STEPS)


def _later_arrival_polls_first(policy: Policy) -> Queue:
    queue = Queue(policy)
    queue.arrive(1, draft=False)
    queue.tick(1)
    queue.arrive(2, draft=False)
    queue.tick(10)
    queue.arrive(3, draft=False)
    queue.tick(21)
    queue.release()
    queue.tick(60)
    return queue


def test_the_race_gives_the_turn_to_the_first_poll_after_a_release():
    queue = _later_arrival_polls_first(Policy.RACE)
    assert queue.holder is not None and queue.holder.pr == 3
    assert queue.waiting[2].overtaken == 1


def test_tickets_give_the_turn_to_the_oldest_waiting_landing():
    queue = _later_arrival_polls_first(Policy.TICKETS)
    assert queue.holder is not None and queue.holder.pr == 2


def _holder_steps_out_then_waits(policy: Policy) -> Queue:
    """A draft's landing steps out for a serial landing, and three drafts arrive meanwhile."""
    queue = Queue(policy)
    queue.arrive(1, draft=True)
    queue.step_out(600)
    queue.arrive(2, draft=True)
    queue.tick(615)
    queue.arrive(3, draft=True)
    queue.arrive(4, draft=True)
    queue.tick(25)
    queue.release()
    queue.tick(30)
    return queue


def test_under_the_race_no_holder_stacks_a_landing_that_stepped_out():
    queue = _holder_steps_out_then_waits(Policy.RACE)
    assert queue.batches[-1] == [3, 4]
    assert queue.waiting[1].overtaken == 1


def test_with_tickets_a_landing_that_stepped_out_takes_the_turn_in_its_order():
    queue = _holder_steps_out_then_waits(Policy.TICKETS)
    assert queue.batches[-1] == [1, 3, 4]


def test_the_models_race_order_is_the_order_the_line_lists_its_places_in(tmp_path):
    prs = [10465, 9998, 10458]
    places = line.here(tmp_path / "line")
    queue = Queue(Policy.RACE)
    queue.arrive(1, draft=True)
    for pr in prs:
        places("join", str(pr), "head")
        queue.arrive(pr, draft=True)
    listed = [
        row.split()[0] for row in places("waiting", str(line.FRESH_S)).splitlines()
    ]
    assert (
        [str(w.pr) for w in queue.stackable()] == listed == ["10458", "10465", "9998"]
    )


@pytest.mark.parametrize(
    ("machine", "reached"),
    [
        (Race, {BATCH_FULL, OVERTAKEN, STEPPED_OUT}),
        (Tickets, {BATCH_FULL, DEFERRED_TO_DEAD, STEPPED_OUT}),
    ],
)
def test_the_generated_queues_reach_every_event(machine, reached):
    """Non-vacuity: a generator that never filled a queue would pass every invariant."""
    seen: set[str] = set()

    class Recording(machine):
        def record(self, event: str) -> None:
            seen.add(event)

    run_state_machine_as_test(Recording, settings=STEPS)
    assert seen == reached
