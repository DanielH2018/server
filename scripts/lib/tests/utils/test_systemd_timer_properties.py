"""Spans and daily calendars read as systemd reads them, over generated input.

The calendar oracle tests each value of each field for membership and shares no code with
`_field`. The bounds it generates inside, and the ones the rejected cases fall outside, are the
ones `systemd-analyze calendar` accepts and refuses. The property tests run under the profile
`tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/lib/tests/utils/test_systemd_timer_properties.py
"""

from itertools import pairwise, product

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from lib.systemd_timer import DAY_S, calendar_fires, longest_gap_s, span_s

# Stated here, not read from the module, so a wrong factor there fails. "" is a bare number.
_UNIT_S = {"h": 3600, "hr": 3600, "min": 60, "m": 60, "s": 1, "sec": 1, "": 1}
_TOPS = (23, 59, 59)


@st.composite
def _spans(draw) -> tuple[str, int]:
    """A span's text and the seconds it names."""
    parts = draw(
        st.lists(
            st.tuples(st.integers(0, 999), st.sampled_from(sorted(_UNIT_S))),
            min_size=1,
            max_size=4,
        )
    )
    text = ""
    for index, (count, unit) in enumerate(parts):
        text += f"{count}{draw(st.sampled_from(['', ' ']))}{unit}"
        if index < len(parts) - 1:
            # A bare number needs a space, or its digits run into the next one's.
            text += draw(st.sampled_from(["", " "] if unit else [" "]))
    return text, sum(count * _UNIT_S[unit] for count, unit in parts)


@given(_spans())
@example(("2hr", 7200))
@example(("30sec", 30))
@example(("1h30", 3630))
@example(("2 hr 5 sec", 7205))
def test_a_span_in_any_listed_unit_is_the_sum_of_its_parts(span):
    text, seconds = span
    assert span_s(text) == seconds


@st.composite
def _part(draw, top: int) -> tuple[str, set[int]]:
    """One in-bounds comma part of a field, and the values it matches."""
    kind = draw(st.sampled_from(["*", "a", "a/step", "a..b", "a..b/step"]))
    step = 1
    if kind == "*":
        text, low, high = "*", 0, top
    elif kind == "a":
        low = high = draw(st.integers(0, top))
        text = f"{low}"
    elif kind == "a/step":
        low = draw(st.integers(0, top - 1))
        high, step = top, draw(st.integers(1, top - low))
        text = f"{low}/{step}"
    else:
        low = draw(st.integers(0, top))
        high = draw(st.integers(low, top))
        text = f"{low}..{high}"
        if kind == "a..b/step":
            step = draw(st.integers(1, top + 1))
            text += f"/{step}"
    matched = {v for v in range(top + 1) if low <= v <= high and (v - low) % step == 0}
    return text, matched


@st.composite
def _calendars(draw) -> tuple[str, list[int]]:
    """A daily `OnCalendar=` expression and every second of the day it fires."""
    texts, matched = [], []
    for top in _TOPS:
        parts = draw(st.lists(_part(top), min_size=1, max_size=3))
        texts.append(",".join(text for text, _ in parts))
        matched.append(sorted(set().union(*(values for _, values in parts))))
    prefix = draw(st.sampled_from(["", "*-*-* "]))
    fires = [h * 3600 + m * 60 + s for h, m, s in product(*matched)]
    return prefix + ":".join(texts), fires


@given(_calendars())
def test_a_calendar_fires_at_each_second_its_fields_match(calendar):
    expression, fires = calendar
    assert calendar_fires(expression) == fires


@given(_calendars(), st.integers(0, 3600))
def test_the_longest_gap_is_the_largest_cyclic_gap_plus_the_delay(calendar, delay):
    expression, fires = calendar
    unit = f"[Timer]\nOnCalendar={expression}\nRandomizedDelaySec={delay}\n"
    gaps = (b - a for a, b in pairwise([*fires, fires[0] + DAY_S]))
    assert longest_gap_s(unit, []) == max(gaps) + delay


@st.composite
def _out_of_bounds(draw) -> str:
    """A daily expression with one comma part systemd refuses, alone or beside a sound one."""
    field = draw(st.integers(0, 2))
    top = _TOPS[field]
    past = draw(st.integers(top + 1, 200_000))
    inside = draw(st.integers(0, top))
    above = draw(st.integers(1, top))
    bad = draw(
        st.sampled_from(
            [
                f"{past}",
                f"0..{past}",
                f"{past}..{past}",
                f"{above}..{above - 1}",
                f"{inside}/{top - inside + 1}",
                f"{inside}..{top}/0",
                f"{inside}/",
                f"*/{above}",
            ]
        )
    )
    fields = ["0", "0", "0"]
    fields[field] = draw(st.sampled_from([bad, f"0,{bad}"]))
    return ":".join(fields)


@given(_out_of_bounds())
@example("24:00")
@example("0..30:00")
@example("1,5..3:00")
@example("*/5:00")
@example("14/10:00")
def test_a_calendar_bound_systemd_refuses_raises(expression):
    with pytest.raises(ValueError, match="out of bounds"):
        calendar_fires(expression)
