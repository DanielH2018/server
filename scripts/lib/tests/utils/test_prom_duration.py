from lib import prom_duration


def test_a_compound_duration_sums_its_components():
    assert prom_duration.seconds("1h30m") == 5400


def test_milliseconds_and_years_are_units():
    assert prom_duration.seconds("1y500ms") == 31536000.5


def test_text_beyond_the_components_is_not_a_duration():
    assert prom_duration.seconds("1h junk") is None


def test_a_unit_outside_the_callers_set_is_not_a_duration():
    assert prom_duration.seconds("90s", units=frozenset("mh")) is None
