"""`merge.await_merge` makes the recorded calls of a merge wait, and only what `step` decides.

`merge_wait_traces.txt` is the characterization corpus: for each seed, a scenario of what every
boundary answers and the trace the merge wait produced for it at the commit that recorded it.
A trace is every boundary call, every printed line and the end, as text
(`_merge_wait_traces.trace`).

A deliberate change of behaviour re-records the corpus, and its PR lists the change:

    RECORD_MERGE_TRACES=1 uv run pytest scripts/deploy_tools/tests/land_merge/test_land_merge_wait_machine.py

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_merge_wait_machine.py
"""

import os
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from _merge_wait_scenarios import Scenario, TurnBy, scenario
from _merge_wait_traces import SEEDS, Run, corpus, modelled, performed, record, trace
from deploy_tools.land_lib.core import landing_actions as a
from deploy_tools.land_lib.core import landing_machine
from deploy_tools.land_lib.core.landing_state import (
    Ci,
    LandingState,
    Point,
    Poll,
    Setup,
)

_SETUP = Setup(pr="1", merge_timeout=300, merge_poll=30, direct=True, stuck_after_s=600)


def _first_difference(want: list, got: list) -> str:
    at = next(
        (i for i, (w, g) in enumerate(zip(want, got, strict=False)) if w != g),
        min(len(want), len(got)),
    )
    return f"line {at}: recorded {want[at : at + 1]}, got {got[at : at + 1]}"


def _runs(sc: Scenario) -> list[Run]:
    """Every merge wait the driver runs for `sc`."""
    with performed() as runs:
        trace(sc)
    return runs


@pytest.fixture(scope="module")
def recorded():
    if os.environ.get("RECORD_MERGE_TRACES"):
        record()
    return corpus()


def test_the_corpus_holds_every_seed(recorded):
    assert [seed for seed, _, _ in recorded] == list(SEEDS)
    for seed, sc, lines in recorded:
        assert sc == scenario(random.Random(seed)), (
            f"seed {seed} draws another scenario"
        )
        # A fake the scenario misconfigured crashes every wait the same way.
        assert not lines[-1].startswith("raised "), f"seed {seed}: {lines[-1]}"


def test_the_driver_reproduces_every_recorded_trace(recorded):
    for seed, sc, want in recorded:
        got = trace(sc)
        assert got == want, f"seed {seed}, {_first_difference(want, got)}"


@given(st.randoms(use_true_random=False))
def test_the_driver_and_the_model_take_the_same_actions(rng):
    """The driver carries out what `step` returns for its answers: all of it, in its order."""
    runs = _runs(scenario(rng))
    assert runs
    for run in runs:
        want = modelled(run)
        assert run.actions == want, _first_difference(want, run.actions)


def test_the_corpus_reaches_every_action_and_every_end(recorded):
    """Non-vacuity: a corpus that never reached a call could not show the driver makes it."""
    done = [x for _, sc, _ in recorded for run in _runs(sc) for x in run.actions]
    seen = {f"end {x.end}" if isinstance(x, a.Die) else type(x).__name__ for x in done}
    actions = {kind.__name__ for kind in a.Action.__value__.__args__} - {"Die"}
    ends = {f"end {end}" for end in a.End}
    assert not (actions | ends) - seen, sorted((actions | ends) - seen)
    assert {sc.turn_by for _, sc, _ in recorded} == set(TurnBy)
    batches = {(sc.batch_red, sc.batch_breaks) for _, sc, _ in recorded if sc.drafts}
    assert {(False, False), (True, False), (False, True)} <= batches, batches


def _green_in_turn() -> LandingState:
    poll = Poll(pr_state="OPEN", head="h1", ci=Ci.GREEN, line="CI green")
    return LandingState(
        setup=_SETUP, awaiting=Point.TESTED, poll=poll, in_turn=True, asked=True
    )


def test_a_head_a_batch_tested_merges_after_a_step_out_of_the_turn():
    """*No discarded proof*: a green batch run on master's tip is never thrown away."""
    _, actions = landing_machine.step(
        _green_in_turn(), a.Tested(tested=True, interrupted=True)
    )
    assert actions == [a.ReadTurnLost()]


def test_an_untested_head_rejoins_after_a_step_out_of_the_turn():
    _, actions = landing_machine.step(
        _green_in_turn(), a.Tested(tested=False, interrupted=True)
    )
    assert actions == [a.RejoinInTurn("h1", conflicting=False)]
