import re

from hypothesis import given
from hypothesis import strategies as st

from lib import prom_duration

_ORACLE = re.compile(r"(\d+y)?(\d+w)?(\d+d)?(\d+h)?(\d+m)?(\d+s)?(\d+ms)?", re.ASCII)
_DESCENDING = ["y", "w", "d", "h", "m", "s", "ms"]


def test_a_compound_duration_sums_its_components():
    assert prom_duration.seconds("1h30m") == 5400


def test_milliseconds_and_years_are_units():
    assert prom_duration.seconds("1y500ms") == 31536000.5


def test_text_beyond_the_components_is_not_a_duration():
    assert prom_duration.seconds("1h junk") is None


def test_units_out_of_descending_order_are_not_a_duration():
    assert prom_duration.seconds("1m1h") is None


def test_a_repeated_unit_is_not_a_duration():
    assert prom_duration.seconds("1s1s") is None


def test_a_non_ascii_digit_is_not_a_duration():
    assert prom_duration.seconds("١s") is None


def test_a_unit_outside_the_callers_set_is_not_a_duration():
    assert prom_duration.seconds("90s", units=frozenset("mh")) is None


@st.composite
def _canonical(draw):
    units = [u for u in _DESCENDING if draw(st.booleans())] or ["s"]
    counts = {u: draw(st.integers(0, 10**6)) for u in units}
    return "".join(f"{n}{u}" for u, n in counts.items()), counts


@given(_canonical())
def test_a_canonical_duration_round_trips(case):
    text, counts = case
    expected = sum(n * prom_duration.UNIT_SECONDS[u] for u, n in counts.items())
    assert prom_duration.seconds(text) == expected


@given(st.text(alphabet="0123456789١ymwdhs ", max_size=12))
def test_seconds_rejects_exactly_what_the_strict_oracle_rejects(text):
    accepted = bool(text) and _ORACLE.fullmatch(text) is not None
    assert (prom_duration.seconds(text) is not None) == accepted
