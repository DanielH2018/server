"""Pin HA's banker's-rounding semantics so the Jinja harness can never silently drift from
Home Assistant's forgiving_round (which fan.jinja's .5-midpoint level math depends on)."""
from jinja_harness import _forgiving_round


def test_half_rounds_to_even_not_away_from_zero():
    # Banker's rounding: ties go to the nearest EVEN integer.
    assert _forgiving_round(0.5) == 0
    assert _forgiving_round(1.5) == 2
    assert _forgiving_round(2.5) == 2
    assert _forgiving_round(3.5) == 4


def test_returns_int_at_precision_zero():
    assert isinstance(_forgiving_round(1.4), int)
    assert _forgiving_round(1.4) == 1
    assert _forgiving_round(1.6) == 2


def test_returns_float_with_precision():
    assert _forgiving_round(1.2345, 2) == 1.23
    assert isinstance(_forgiving_round(1.2345, 2), float)
