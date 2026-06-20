"""Unit tests for the bedroom lighting macros in custom_templates/lighting.jinja."""
from jinja_harness import render_macro

LIGHT = "lighting.jinja"


def _window(elapsed):
    return render_macro(LIGHT, "in_wake_window", elapsed)


def _brightness(elapsed, sleep_min):
    return int(render_macro(LIGHT, "wake_brightness", elapsed, sleep_min))


def _transition(elapsed):
    return int(render_macro(LIGHT, "wake_transition", elapsed))


def _allowed(in_window, illuminance):
    return render_macro(LIGHT, "auto_light_allowed", in_window, illuminance)


def test_in_wake_window_boundaries():
    assert _window(0) == "True"
    assert _window(7.5) == "True"
    assert _window(14.99) == "True"
    assert _window(15) == "False"      # strict upper bound (window ends AT the alarm)
    assert _window(-1) == "False"      # unavailable-sensor sentinel


def test_wake_brightness_ramp_endpoints():
    assert _brightness(0, 0) == 1      # 1% at window start
    assert _brightness(15, 0) == 50    # full peak at the alarm (normal night)


def test_wake_brightness_short_night_lowers_peak():
    assert _brightness(15, 300) == 30  # 0 < 300 < 360 -> gentler 30% peak
    assert _brightness(15, 0) == 50    # unknown/0 sleep -> normal 50%
    assert _brightness(15, 400) == 50  # long night -> normal 50%


def test_wake_brightness_is_monotonic():
    vals = [_brightness(e, 0) for e in range(0, 16)]
    assert vals == sorted(vals)
    assert vals[0] == 1 and vals[-1] == 50


def test_wake_transition_counts_down_seconds():
    assert _transition(0) == 900       # full 15 min remaining
    assert _transition(7.5) == 450
    assert _transition(15) == 0


def test_auto_light_allowed_truth_table():
    assert _allowed(True, 1000) == "True"   # in-window wakes regardless of brightness
    assert _allowed(False, 40) == "True"    # dark enough
    assert _allowed(False, 74) == "True"
    assert _allowed(False, 75) == "False"   # strict < 75
    assert _allowed(False, 80) == "False"
