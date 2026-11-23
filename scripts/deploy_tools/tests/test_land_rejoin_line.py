"""A green ready PR that master moved past goes back in the line for the turn as a draft.

Run: uv run pytest scripts/deploy_tools/tests/test_land_rejoin_line.py
"""

import pytest

from deploy_tools.land_lib import batch, turn
from deploy_tools.land_lib.outcome import Outcome
from _land_turn_fakes import (
    A_RUN,
    HEAD,
    MASTER_MOVED,
    MERGED,
    OPEN,
    UPDATED,
    land_in_turn,
    line_calls,
    merged_sha,
    call_order,
    turn_landing,
)

READY = {**OPEN, "isDraft": False}
# The PR as `stale_base.rejoin_line` leaves it: a draft at the head the update made.
REJOINED = {**OPEN, "isDraft": True, "headRefOid": UPDATED}
SKIPPED_RUN = [{"id": 3, "status": "completed", "conclusion": "skipped"}]


def _ran_then_skipped(head: str) -> list:
    """A ready run that ran jobs on the PR's own head, and only a skipped draft run after."""
    return A_RUN if head == HEAD else SKIPPED_RUN


def _undo_calls(calls) -> int:
    return sum(1 for name, args, _ in calls if name == "gh" and "--undo" in args)


def test_a_green_ready_head_master_moved_past_waits_in_the_line_as_a_draft(landing):
    updated = {**OPEN, "headRefOid": UPDATED}
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, updated, updated, {**MERGED, "headRefOid": UPDATED}),
        start=(READY, REJOINED, REJOINED),
        head_runs=_ran_then_skipped,
        compare=[["docs/other.md"], []],
        turn=[False, True],
    )
    land_in_turn(ln)
    assert _undo_calls(calls) == 1
    assert call_order(calls) == [
        "pr ready",
        "update",
        "try_turn",
        "try_turn",
        "pr ready",
        "merge",
        "release_turn",
    ]
    assert line_calls(calls)[0] == ("join", "999", UPDATED)
    assert merged_sha(calls) == UPDATED


def test_a_ready_head_master_moved_past_while_it_waited_gives_the_turn_back(
    landing, capsys
):
    # fact: docs/ci-throughput.md#What B4 measured, and D10
    updated = {**OPEN, "headRefOid": UPDATED}
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, OPEN, updated, updated, {**MERGED, "headRefOid": UPDATED}),
        start=(READY, REJOINED, REJOINED),
        head_runs=_ran_then_skipped,
        compare=[[], ["docs/other.md"], []],
        turn=[False, True, True],
    )
    land_in_turn(ln)
    order = call_order(calls)
    assert _undo_calls(calls) == 1
    gave_back = order.index("release_turn")
    # It asks for the turn again as a draft, so its run is the one its next turn starts.
    assert "try_turn" in order[gave_back : order.index("merge")]
    assert merged_sha(calls) == UPDATED
    assert "gave the landing turn back" in capsys.readouterr().out


def test_a_ready_head_master_moved_past_while_a_serial_landing_ended_rejoins_the_line(
    landing,
):
    updated = {**OPEN, "headRefOid": UPDATED}
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, updated, updated, {**MERGED, "headRefOid": UPDATED}),
        start=(READY, REJOINED),
        head_runs=_ran_then_skipped,
        compare=[[], ["docs/other.md"], []],
        serial=[False, True],
    )
    land_in_turn(ln)
    assert _undo_calls(calls) == 1
    assert merged_sha(calls) == UPDATED


def test_a_ready_head_master_left_alone_while_it_waited_keeps_the_turn(landing):
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, OPEN, MERGED),
        start=READY,
        head_runs=_ran_then_skipped,
        compare=[[], []],
        turn=[False, True],
    )
    land_in_turn(ln)
    assert _undo_calls(calls) == 0
    assert call_order(calls)[-2:] == ["merge", "release_turn"]


def test_a_rejoined_head_whose_draft_run_is_still_running_waits_in_the_line(landing):
    updated = {**OPEN, "headRefOid": UPDATED}
    running = [{"id": 3, "status": "in_progress", "conclusion": None}]
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, updated, updated, {**MERGED, "headRefOid": UPDATED}),
        start=(READY, REJOINED, REJOINED),
        head_runs=lambda head: A_RUN if head == HEAD else running,
        compare=[["docs/other.md"], []],
        turn=[False, True],
    )
    land_in_turn(ln)
    assert line_calls(calls)[0] == ("join", "999", UPDATED)
    order = call_order(calls)
    assert order.index("try_turn") < order.index("pr ready", 1)


def test_a_rejoined_head_master_moved_past_again_merges_only_its_turns_update(landing):
    final = "c" * 40
    ln, calls = turn_landing(
        landing,
        polls=(
            OPEN,
            {**OPEN, "headRefOid": UPDATED},
            {**OPEN, "headRefOid": UPDATED},
            {**OPEN, "headRefOid": final},
            {**OPEN, "headRefOid": final},
            {**MERGED, "headRefOid": final},
        ),
        start=(READY, REJOINED),
        head_runs=_ran_then_skipped,
        compare=[["docs/other.md"], ["docs/later.md"]],
    )
    land_in_turn(ln)
    assert call_order(calls).count("update") == 2
    assert merged_sha(calls) == final


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"require_head": HEAD}, id="a-required-head"),
        pytest.param({"approved_head": HEAD}, id="an-approved-head"),
    ],
)
def test_a_green_ready_head_that_cannot_rejoin_asks_for_the_turn_as_it_stands(
    landing, case
):
    approved = case.pop("approved_head", "")
    ln, calls = turn_landing(
        landing,
        **{
            "start": READY,
            "head_runs": _ran_then_skipped,
            "compare": MASTER_MOVED,
            **case,
        },
    )
    ln.approved_head = approved
    land_in_turn(ln)
    assert _undo_calls(calls) == 0
    assert merged_sha(calls) == HEAD


def test_a_conflicting_ready_head_is_not_converted_to_draft(landing):
    # GitHub serves CONFLICTING for one poll when master moves under a PR, so it reaches here.
    conflicting = {**OPEN, "mergeable": "CONFLICTING"}
    ln, calls = turn_landing(
        landing,
        polls=(conflicting, MERGED),
        start=READY,
        head_runs=_ran_then_skipped,
        compare=MASTER_MOVED,
    )
    land_in_turn(ln)
    assert _undo_calls(calls) == 0


def test_a_refused_update_after_the_draft_conversion_marks_the_pr_ready_again(
    landing, capsys
):
    # gh answers: the conversion, the refused update, the ready flip, then the merge.
    ln, calls = turn_landing(
        landing,
        start=READY,
        head_runs=_ran_then_skipped,
        compare=MASTER_MOVED,
        gh_merge_rc=[0, 1, 0, 0],
    )
    land_in_turn(ln)
    assert _undo_calls(calls) == 1
    assert call_order(calls)[:3] == ["pr ready", "update", "pr ready"]
    assert merged_sha(calls) == HEAD
    assert "(boom); marking it ready again" in capsys.readouterr().out


def test_a_rejoined_pr_a_rule_refuses_in_the_turn_is_marked_ready_again(landing):
    ln, calls = turn_landing(
        landing,
        polls=(OPEN, {**OPEN, "headRefOid": UPDATED}),
        start=(READY, REJOINED),
        head_runs=_ran_then_skipped,
        compare=[["docs/other.md"]],
        serial=[False],
    )
    with pytest.raises(Outcome) as stop, turn.held(ln) as take_turn:
        batch.land(ln, take_turn)
    assert stop.value.rc == 75
    assert _undo_calls(calls) == 1
    assert call_order(calls) == [
        "pr ready",
        "update",
        "try_turn",
        "release_turn",
        "try_turn",
        "pr ready",
        "release_turn",
    ]
