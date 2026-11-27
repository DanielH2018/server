"""The landing slots over real flock files: a take succeeds exactly while fewer than its cap are held.

Each take is one landing. It asks for a slot under a cap of 1 or `slots.COUNT`, among every
slot name or the first `slots.FALLBACK_COUNT` that a host counts alone, through `slots.try_take`
or `turn.lock_below`. A release frees one slot a take returned. Every take opens its own file
descriptions, so two takes in one process conflict the way two landings do. After every step
the machine reads which files are locked from the files themselves, with a probe of its own.
The seed and the example count come from `tests/hypothesis_profiles.py`; only the sequence
length is set here, because every step probes each slot file.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_slots_properties.py
"""

import fcntl
import os
from functools import partial
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    multiple,
    rule,
    run_state_machine_as_test,
)

from deploy_tools.land_lib import slots, turn

STEPS = settings(stateful_step_count=25)
CAP_1_REFUSED = "cap 1 refused a take"
CAP_1_TOOK = "cap 1 let a take through"
FALLBACK_REFUSED = "the fallback slots refused a take"
RETAKEN = "a released slot was taken again"


def _take(via: str, paths: list[Path], cap: int) -> slots.Release | None:
    if via == "try_take":
        return slots.try_take(paths, cap)
    fd = turn.lock_below([str(p) for p in paths], cap)
    return None if fd is None else partial(os.close, fd)


def _locked(paths: list[Path]) -> set[int]:
    """The indexes of the files another descriptor holds locked, read without keeping a lock."""
    locked = set()
    for n, path in enumerate(paths):
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked.add(n)
        finally:
            os.close(fd)
    return locked


class SlotHolders(RuleBasedStateMachine):
    """Landings taking and releasing the slot files in `directory`, which a test binds."""

    held = Bundle("held")
    directory: Path

    def __init__(self):
        super().__init__()
        self.paths = slots._paths(self.directory)
        self.releases: dict[int, slots.Release] = {}
        self.released: set[int] = set()

    def record(self, event: str) -> None:
        """A hook for the coverage test."""

    @rule(
        target=held,
        cap=st.sampled_from([1, slots.COUNT]),
        count=st.sampled_from([slots.COUNT, slots.FALLBACK_COUNT]),
        via=st.sampled_from(["try_take", "lock_below"]),
    )
    def take(self, cap, count, via):
        before = set(self.releases)
        counted = len(before & set(range(count)))
        release = _take(via, self.paths[:count], cap)
        taken = _locked(self.paths) - before
        assert (release is not None) == (counted < min(cap, count)), (
            f"{via} under cap {cap} among {count} with {sorted(before)} held"
        )
        if release is None:
            assert not taken, f"a refused take left {sorted(taken)} locked"
            self.record(CAP_1_REFUSED if cap == 1 else FALLBACK_REFUSED)
            return multiple()
        assert len(taken) == 1, f"one take locked {sorted(taken)}"
        (n,) = taken
        assert n == min(set(range(count)) - before)
        if cap == 1:
            self.record(CAP_1_TOOK)
        if n in self.released:
            self.record(RETAKEN)
        self.releases[n] = release
        return n

    @rule(slot=consumes(held))
    def release(self, slot):
        before = _locked(self.paths)
        self.releases.pop(slot)()
        self.released.add(slot)
        assert before - _locked(self.paths) == {slot}

    @invariant()
    def the_files_hold_what_the_takes_returned_and_no_more_than_the_largest_cap(self):
        locked = _locked(self.paths)
        assert locked == set(self.releases)
        assert len(locked) <= slots.COUNT

    def teardown(self):
        for release in self.releases.values():
            release()


def test_a_take_succeeds_exactly_while_fewer_than_its_cap_are_held(tmp_path):
    class InTmpPath(SlotHolders):
        directory = tmp_path

    run_state_machine_as_test(InTmpPath, settings=STEPS)


def test_the_generated_takes_reach_both_caps_the_fallback_and_a_retake(tmp_path):
    """Non-vacuity: a generator that never filled a cap would pass every invariant."""
    seen: set[str] = set()

    class Recording(SlotHolders):
        directory = tmp_path

        def record(self, event: str) -> None:
            seen.add(event)

    run_state_machine_as_test(Recording, settings=STEPS)
    assert seen == {CAP_1_REFUSED, CAP_1_TOOK, FALLBACK_REFUSED, RETAKEN}
