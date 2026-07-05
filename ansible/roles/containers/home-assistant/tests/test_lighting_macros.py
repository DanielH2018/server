"""Unit tests for the bedroom lighting macros in custom_templates/lighting.jinja."""

from jinja_harness import render_macro

LIGHT = "lighting.jinja"


def _window(elapsed):
    return render_macro(LIGHT, "in_wake_window", elapsed)


def _release_window(elapsed):
    return render_macro(LIGHT, "in_wake_release_window", elapsed)


def _brightness(elapsed, sleep_min):
    return int(render_macro(LIGHT, "wake_brightness", elapsed, sleep_min))


def _allowed(in_window, illuminance):
    return render_macro(LIGHT, "auto_light_allowed", in_window, illuminance)


def test_in_wake_window_boundaries():
    assert _window(0) == "True"
    assert _window(15) == "True"  # the alarm is 1/3 in, not the end
    assert (
        _window(30) == "True"
    )  # alarm+15 is still mid-window (window is unchanged at [0,45))
    assert _window(44.99) == "True"
    assert _window(45) == "False"  # window ends 30 min AFTER the alarm
    assert _window(-1) == "False"  # unavailable-sensor sentinel


def test_in_wake_release_window_is_a_bounded_catchup():
    # The release catch-up starts exactly where in_wake_window ends (45) so there's no gap or overlap,
    # and runs for 45 more min so a missed single-tick hand-off self-heals after a restart/deploy.
    assert (
        _release_window(44.99) == "False"
    )  # still inside the ramp -> not releasing yet
    assert _release_window(45) == "True"  # window end: hand-off becomes due
    assert _release_window(60) == "True"  # mid catch-up (covers a long boot)
    assert _release_window(89.99) == "True"
    # Past the catch-up it stops policing AL, so a deliberate daytime manual pick is never stomped.
    assert _release_window(90) == "False"
    assert _release_window(300) == "False"  # hours later
    assert _release_window(-1) == "False"  # no morning alarm / sensor unavailable


def test_wake_brightness_curve_endpoints():
    assert _brightness(0, 0) == 1  # 1% at window start (alarm-15)
    assert _brightness(15, 0) == 8  # ~8% at the alarm (soft, non-jarring)
    assert (
        _brightness(35, 0) == 20
    )  # still-dim knee at alarm+20 -> stays gentle past the alarm
    assert _brightness(45, 0) == 100  # 100% at alarm+30 -> seamless AL hand-off, no pop


def test_wake_brightness_is_gentle_then_steep():
    # Each segment is steeper than the last, with the climb pushed into the final 10 min:
    # pre-alarm 7%/15min, alarm->knee 12%/20min (a dim plateau), knee->full 80%/10min.
    assert _brightness(7.5, 0) == 4  # 1 + (8-1)*0.5 = 4.5 -> banker's round -> 4
    assert _brightness(25, 0) == 14  # 8 + (20-8)*(10/20) -> ~06:10 stays dim
    assert (
        _brightness(40, 0) == 60
    )  # 20 + (100-20)*(5/10) -> the steep tail near window end


def test_wake_brightness_short_night_lowers_curve():
    assert _brightness(15, 300) == 5  # 0 < 300 < 360 -> gentler ~5% at the alarm
    assert _brightness(35, 300) == 14  # ...and ~14% knee
    assert (
        _brightness(45, 300) == 100
    )  # ...but STILL reaches 100% (else the AL pop returns)
    assert _brightness(15, 0) == 8  # unknown/0 sleep -> normal
    assert _brightness(15, 400) == 8  # long night -> normal


def test_auto_light_allowed_truth_table():
    assert _allowed(True, 1000) == "True"  # in-window wakes regardless of brightness
    assert _allowed(False, 40) == "True"  # dark enough
    assert _allowed(False, 74) == "True"
    assert _allowed(False, 75) == "False"  # strict < 75
    assert _allowed(False, 80) == "False"


def _natural(hour, illuminance):
    return int(render_macro(LIGHT, "natural_brightness", hour, illuminance))


def test_natural_brightness_time_bands_dark_room():
    assert _natural(7, 0) == 55  # morning base, dark room -> factor 1.0
    assert _natural(12, 0) == 45  # daytime base
    assert _natural(20, 0) == 35  # evening base


def test_natural_brightness_dims_with_ambient():
    assert _natural(12, 75) == 9  # at the gate ceiling: 45 * 0.2
    assert _natural(12, 750) == 9  # above the gate: factor clamps at 0.2
    assert _natural(20, 0) > _natural(20, 70)  # brighter room -> dimmer output


def test_natural_brightness_deep_night_falls_back_low():
    assert _natural(3, 0) == 35  # 00:00-05:00 is the nightlight path; fallback base


def _decision(
    reason,
    manual_off=False,
    sleep_mode=False,
    person_home=True,
    presence=True,
    lux_allowed=True,
    light_on=False,
):
    return render_macro(
        LIGHT,
        "light_decision",
        reason,
        manual_off,
        sleep_mode,
        person_home,
        presence,
        lux_allowed,
        light_on,
    )


def test_light_decision_presence_all_gates_pass():
    assert _decision("presence") == "natural"


def test_light_decision_presence_each_gate_blocks():
    assert _decision("presence", manual_off=True) == "noop"
    assert _decision("presence", sleep_mode=True) == "noop"
    assert _decision("presence", person_home=False) == "noop"
    assert _decision("presence", presence=False) == "noop"
    assert _decision("presence", lux_allowed=False) == "noop"
    assert _decision("presence", light_on=True) == "noop"  # never re-stomp an on light


def test_light_decision_passthrough_reasons_are_ungated():
    # natural/wake/wake_fallback/off ignore the flags (the caller already gated).
    assert _decision("natural", manual_off=True, person_home=False) == "natural"
    assert _decision("wake", lux_allowed=False) == "wake"
    assert _decision("off", light_on=True) == "off"


def test_light_decision_wake_fallback_is_passthrough():
    # The 06:00 safety-net ramp routes through the single writer as its own ungated reason
    # (bedroom_fallback_wake pre-gates); it must pass through, not fall into the noop bucket.
    assert _decision("wake_fallback") == "wake_fallback"
    assert (
        _decision("wake_fallback", manual_off=True, presence=False) == "wake_fallback"
    )


def test_light_decision_unknown_reason_is_noop():
    assert _decision("bogus") == "noop"


def _exception(sleep_mode, hour, in_window):
    return render_macro(LIGHT, "natural_exception", sleep_mode, hour, in_window)


def test_natural_exception_selection():
    assert _exception(True, 23, False) == "nightlight"  # sleep mode, outside window
    assert _exception(False, 3, False) == "nightlight"  # deep night 00:00-05:00
    assert _exception(False, 12, False) == "default"  # daytime, no exception
    assert _exception(False, 7, True) == "wake"  # morning ramp window


def test_natural_exception_early_alarm_yields_to_wake():
    # The documented trap: an early alarm puts hour<5 INSIDE the window -> must be `wake`, not the
    # 3% nightlight (which would mask the ramp).
    assert _exception(False, 4, True) == "wake"
    assert _exception(True, 4, True) == "wake"  # even in sleep mode, the window wins
    assert _exception(False, 5, False) == "default"  # strict hour < 5 boundary


def _away_label(light_on, fan_on):
    return render_macro(LIGHT, "away_items_label", light_on, fan_on)


def test_away_items_label_truth_table():
    assert _away_label(True, True) == "lights + fan"
    assert _away_label(True, False) == "lights"
    assert _away_label(False, True) == "fan"
    assert _away_label(False, False) == ""  # nothing on -> gate stays silent


def _arrive(presence, manual_off, light_on):
    return render_macro(LIGHT, "arrive_relight_allowed", presence, manual_off, light_on)


def test_arrive_relight_allowed_truth_table():
    assert (
        _arrive(True, False, False) == "True"
    )  # present, not blocked, lights off -> relight
    assert _arrive(False, False, False) == "False"  # not in the room
    assert _arrive(True, True, False) == "False"  # manual-off engaged
    assert _arrive(True, False, True) == "False"  # already on -> never re-stomp
