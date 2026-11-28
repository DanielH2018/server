"""A serial PR's yield of the landing turn to the drafts standing in the line.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_slots_yield.py
"""

from dataclasses import replace

import pytest

from _land_turn_fakes import HEAD, UPDATED, call_order
from deploy_tools.land_lib import line, slots
from test_land_slots import _SERIAL, _land, _merging, _names

_WAITER = "1001"


def _waiter_in_the_line(ln, tmp_path, leaves_after=None):
    """Stand PR 1001 in a real line for `ln`; it leaves at sleep number `leaves_after`."""
    the_line = line.here(tmp_path)
    the_line("join", _WAITER, UPDATED)
    sleep, slept = ln.tools.sleep, []

    def poll(seconds):
        sleep(seconds)
        slept.append(seconds)
        if len(slept) == leaves_after:
            the_line("leave", _WAITER)

    ln.tools = replace(ln.tools, line=the_line, sleep=poll)


def _slept_before_the_serial_lock(ln, calls) -> int:
    names = _names(calls)
    return names[: names.index("try_serial")].count("sleep") * ln.opts.merge_poll


@pytest.mark.parametrize("draft", [True, False], ids=["a-draft", "a-ready-head"])
def test_a_serial_pr_yields_the_turn_until_the_line_empties(landing, tmp_path, draft):
    ln, calls = _merging(landing, _SERIAL, draft)
    _waiter_in_the_line(ln, tmp_path, leaves_after=3)
    _land(ln)
    names = _names(calls)
    assert names.index("release_turn") < names.index("try_serial")
    assert _slept_before_the_serial_lock(ln, calls) == 3 * ln.opts.merge_poll
    assert "merge" in call_order(calls)


def test_a_serial_pr_takes_the_serial_lock_only_once_it_holds_the_turn_again(
    landing, tmp_path
):
    # The waiter leaves the line as it takes the turn, and holds the turn for two more polls.
    ln, calls = _merging(landing, _SERIAL, draft=True, turn=[True, False, False, True])
    _waiter_in_the_line(ln, tmp_path, leaves_after=1)
    _land(ln)
    names = _names(calls)
    retaken = [i for i, name in enumerate(names) if name == "try_turn"][3]
    assert names.index("try_serial") > retaken
    assert "merge" in call_order(calls)


def test_a_serial_pr_takes_the_turn_once_one_merge_budget_is_left(landing, tmp_path):
    ln, calls = _merging(landing, _SERIAL, draft=True)
    _waiter_in_the_line(ln, tmp_path)
    _land(ln)
    slept = _slept_before_the_serial_lock(ln, calls)
    # The fake clock also ticks once per read, which spends some of the budget.
    assert 0.9 * ln.opts.merge_timeout < slept <= ln.opts.merge_timeout
    assert "merge" in call_order(calls)


@pytest.mark.parametrize(
    "fakes",
    [{"line": [OSError("ssh daniel-box: no route")]}, {"in_window": True}],
    ids=["an-unreadable-line", "inside-the-window"],
)
def test_a_serial_pr_that_cannot_yield_takes_the_turn_at_once(landing, tmp_path, fakes):
    ln, calls = _merging(landing, _SERIAL, draft=True, **fakes)
    if "in_window" in fakes:
        _waiter_in_the_line(ln, tmp_path)
    _land(ln)
    assert "sleep" not in _names(calls)
    assert "merge" in call_order(calls)


def test_a_serial_pr_holding_the_serial_lock_never_yields(landing, tmp_path):
    ln, calls = _merging(landing, _SERIAL)
    _waiter_in_the_line(ln, tmp_path)
    ln.serial_lock = lambda: None
    ln.give_back_turn = lambda: None
    slots.refuse_before_merge(ln, HEAD)
    assert "sleep" not in _names(calls)
