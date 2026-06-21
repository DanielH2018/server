"""Unit tests for the bedroom lighting macros in custom_templates/lighting.jinja."""
from jinja_harness import render_macro

LIGHT = "lighting.jinja"


def _window(elapsed):
    return render_macro(LIGHT, "in_wake_window", elapsed)


def _brightness(elapsed, sleep_min):
    return int(render_macro(LIGHT, "wake_brightness", elapsed, sleep_min))


def _allowed(in_window, illuminance):
    return render_macro(LIGHT, "auto_light_allowed", in_window, illuminance)


def test_in_wake_window_boundaries():
    assert _window(0) == "True"
    assert _window(15) == "True"       # the alarm is now mid-window, not the end
    assert _window(29.99) == "True"
    assert _window(30) == "False"      # window ends 15 min AFTER the alarm
    assert _window(-1) == "False"      # unavailable-sensor sentinel


def test_wake_brightness_curve_endpoints():
    assert _brightness(0, 0) == 1      # 1% at window start (alarm-15)
    assert _brightness(15, 0) == 12    # ~12% at the alarm (gentle pre-alarm)
    assert _brightness(30, 0) == 40    # 40% peak at alarm+15 (the "get up" push)


def test_wake_brightness_is_gentle_then_steep():
    # Post-alarm slope (28% over 15 min) is steeper than pre-alarm (11% over 15 min).
    assert _brightness(22.5, 0) == 26  # 12 + (40-12)*0.5
    assert _brightness(7.5, 0) == 6    # 1 + (12-1)*0.5 = 6.5 -> banker's round -> 6


def test_wake_brightness_short_night_lowers_curve():
    assert _brightness(15, 300) == 7   # 0 < 300 < 360 -> gentler ~7% at the alarm
    assert _brightness(30, 300) == 24  # ...and ~24% peak
    assert _brightness(15, 0) == 12    # unknown/0 sleep -> normal
    assert _brightness(15, 400) == 12  # long night -> normal


def test_auto_light_allowed_truth_table():
    assert _allowed(True, 1000) == "True"   # in-window wakes regardless of brightness
    assert _allowed(False, 40) == "True"    # dark enough
    assert _allowed(False, 74) == "True"
    assert _allowed(False, 75) == "False"   # strict < 75
    assert _allowed(False, 80) == "False"
