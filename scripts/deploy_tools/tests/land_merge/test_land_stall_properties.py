"""The merge wait never sits on a stalled head past the grace, over generated poll sequences.

Each poll reports a head, the stall await_ci named on it, whether the PR is ready and whether a run
that can still give a verdict exists. The machine checks every poll's action against what the
landing turn owes the landings behind it. The seed and the example count come from
`tests/hypothesis_profiles.py`; only the sequence length is set here, because a give-up needs two
graces of polls.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_stall_properties.py
"""

from collections import Counter

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    precondition,
    rule,
    run_state_machine_as_test,
)

from deploy_tools.land_lib import stall
from deploy_tools.land_lib.stall import Act

STEPS = settings(stateful_step_count=40)
_POLLS = st.sampled_from([20, 60, stall.GRACE_S])
_STARTS = {Act.TOGGLE, Act.RERUN}


class StallWait(RuleBasedStateMachine):
    """One merge wait, polled by Hypothesis, with the facts each poll reads."""

    def __init__(self):
        super().__init__()
        self.start_landing()

    def start_landing(self) -> None:
        """A new wait. A give-up ends the landing, so the next poll belongs to another one."""
        self.watch = stall.Watch()
        self.waited = 0
        # The last poll at which the landing had a reason not to act: a new stall, a draft PR,
        # a run in progress, or an action of its own.
        self.excused_at = 0
        self.last: tuple[str, stall.Stall] | None = None
        self.started: Counter[tuple[str, stall.Stall]] = Counter()

    def record(self, act: Act) -> None:
        """A hook for the coverage test."""

    @rule(
        head=st.sampled_from(["aaaa1111", "bbbb2222"]),
        kind=st.sampled_from([None, *stall.Stall]),
        ready=st.booleans(),
        running=st.sampled_from(["", "run 9 is in_progress"]),
        poll=_POLLS,
    )
    def poll(self, head, kind, ready, running, poll):
        self.observe(head, kind, ready, running, poll)

    @precondition(lambda self: self.last is not None)
    @rule(poll=_POLLS)
    def same_stall_on_an_idle_ready_pr(self, poll):
        assert self.last is not None
        self.observe(*self.last, True, "", poll)

    def observe(self, head, kind, ready, running, poll) -> None:
        self.waited += poll
        reads: list[str] = []

        def is_ready() -> bool:
            reads.append("ready")
            return ready

        def is_running() -> str:
            reads.append("running")
            return running

        self.watch, act = stall.step(
            self.watch, head, kind, self.waited, is_ready, is_running
        )
        self.record(act)
        key = (head, kind) if kind is not None else None
        idle = key is not None and key == self.last and ready and not running

        if idle and self.waited - self.excused_at >= stall.GRACE_S:
            assert act in _STARTS | {Act.GIVE_UP}, (
                f"{act} after {self.waited - self.excused_at}s on an idle stall"
            )
        if act in _STARTS:
            assert key is not None and kind is not None and ready and not running
            assert act is stall.START[kind]
            self.started[key] += 1
            assert self.started[key] == 1, f"a second {act} for {key}"
        if act in (Act.WAIT, Act.ANNOUNCE) and (kind is None or act is Act.ANNOUNCE):
            assert reads == [], "read GitHub before the grace ran out"
        if act is Act.GIVE_UP:
            assert key is not None and self.started[key] == 1, "gave up without trying"
            self.start_landing()
            return
        if not idle or act in _STARTS:
            self.excused_at = self.waited
        self.last = key


TestStallWait = StallWait.TestCase
TestStallWait.settings = STEPS


def test_the_generated_polls_reach_every_action():
    """Non-vacuity: a generator that never outlasted the grace would pass every invariant."""
    seen: set[Act] = set()

    class Recording(StallWait):
        def record(self, act: Act) -> None:
            seen.add(act)

    run_state_machine_as_test(Recording, settings=STEPS)
    assert seen == set(Act)
