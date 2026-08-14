"""Unit tests for the DREO fan macros in custom_templates/fan.jinja."""

import math

from jinja_harness import _forgiving_round, render_macro

FAN = "fan.jinja"


def _level(pct):
    return int(render_macro(FAN, "pct_to_level", pct))


def _pct(level):
    return int(render_macro(FAN, "level_to_pct", level))


def _target(temp_f, cur_level, is_night, sleep, outdoor_temp_f=-999):
    return int(
        render_macro(
            FAN, "fan_target_level", temp_f, cur_level, is_night, sleep, outdoor_temp_f
        )
    )


def test_send_pct_ceils_to_target_level():
    # The real fan.jinja promise: level_to_pct sends the MIDPOINT of a level's range, and the DREO
    # integration math.ceil()s the requested % up to a discrete level — landing exactly on L. (This
    # is NOT pct_to_level(level_to_pct(L)): pct_to_level expects the fan's REPORTED %, not the send %.)
    for level in range(1, 10):
        assert math.ceil(_pct(level) * 9 / 100) == level, (
            f"send% for L{level} does not ceil to it"
        )


def test_reported_pct_recovers_level():
    # pct_to_level maps the fan's REPORTED percentage back to its level (so bedroom_fan_manual_detect
    # can compare our commanded level against the cloud echo). A 9-speed fan reports round(L*100/9)%.
    for level in range(1, 10):
        reported = _forgiving_round(level * 100 / 9)
        assert _level(reported) == level, f"reported% for L{level} does not recover it"


def test_level_zero_is_off_both_ways():
    assert _pct(0) == 0
    assert _level(0) == 0


def test_off_below_start_temperature():
    assert _target(71.0, 0, False, False) == 0  # ideal 0 -> off
    assert _target(70.0, 3, False, False) == 0  # cold even with a fan already running


def test_unavailable_sensor_sentinel_is_off():
    assert _target(-1.0, 5, False, False) == 0  # t < 0 -> off (sensor unavailable)


def test_curve_low_and_high_ends():
    assert _target(72.0, 0, False, False) == 1  # (72-71)/1.3 = 0.77 -> 1
    assert _target(83.0, 0, False, False) == 9  # (83-71)/1.3 = 9.23 -> 9


def test_curve_clamps_at_max_level():
    assert _target(90.0, 0, False, False) == 9  # ideal ~14.6, capped to 9


def test_hysteresis_holds_within_deadband():
    # ideal 5.4 with cur_level 5 is within +/-0.7 -> no step.
    assert _target(78.02, 5, False, False) == 5


def test_hysteresis_steps_outside_deadband():
    # ideal 5.85 with cur_level 5 exceeds +0.7 -> step up.
    assert _target(78.6, 5, False, False) == 6


def test_night_cap_limits_to_level_4():
    assert _target(83.0, 0, True, False) == 4


# Sleep-mode seasonal floor + fixed L5 ceiling, keyed off OUTDOOR temp: outdoor picks the floor band
# (winter 2 / shoulder 3 / summer 4), the indoor curve modulates within [floor, 5]. Replaces the old
# flat L2 sleep cap.
def test_sleep_summer_floor_and_ceiling():
    # Summer (outdoor >= 68): floor L4, ceiling L5.
    assert (
        _target(83.0, 0, False, True, 80.0) == 5
    )  # hot room, curve ~9.2 -> L5 ceiling
    assert _target(74.0, 0, False, True, 80.0) == 4  # warm room, curve ~2.3 -> L4 floor


def test_sleep_winter_floor_holds_white_noise():
    # Winter (outdoor < 45): floor L2. A cold room's curve wants 0 -> the floor keeps it at L2, not off.
    assert _target(69.0, 0, False, True, 30.0) == 2
    assert (
        _target(78.0, 0, False, True, 30.0) == 5
    )  # hot room still capped at the L5 ceiling


def test_sleep_shoulder_floor():
    # Spring/fall (45 <= outdoor < 68): floor L3.
    assert _target(70.0, 0, False, True, 55.0) == 3  # cool room -> L3 floor
    assert _target(76.0, 0, False, True, 55.0) == 4  # curve ~3.85 -> L4, within [3, 5]


def test_sleep_floor_band_boundaries():
    # Bands are half-open at 45 and 68 °F. Cold room (curve wants 0) so the FLOOR is the output.
    assert _target(69.0, 0, False, True, 44.9) == 2  # just below 45 -> winter floor
    assert _target(69.0, 0, False, True, 45.0) == 3  # 45 -> shoulder floor
    assert _target(69.0, 0, False, True, 67.9) == 3  # just below 68 -> shoulder floor
    assert _target(69.0, 0, False, True, 68.0) == 4  # 68 -> summer floor


def test_sleep_missing_outdoor_falls_back_to_winter_band():
    # Outdoor unavailable (default sentinel) -> winter band (floor L2 = the old quiet sleep behavior).
    assert _target(69.0, 0, False, True) == 2  # cold room -> L2 floor
    assert _target(83.0, 0, False, True) == 5  # hot room -> L5 ceiling


# Migration safety net: the extracted macro must equal the ORIGINAL inline bedroom_apply_fan formula
# for every NON-sleep input. This pins behavior-preservation of the curve + night cap. The sleep
# branch intentionally diverges from the old flat L2 cap (seasonal floor/ceiling — covered by the
# dedicated tests above), so it is excluded here.
def _inline_target(t, cur_level, is_night):
    # The pre-extraction non-sleep formula, transcribed from scripts.yaml's bedroom_apply_fan.
    ideal = (t - 71) / 1.3 if t >= 0 else 0
    cap = 4 if is_night else 9
    if t < 0 or ideal < 0.3:
        want = 0
    elif cur_level == 0 or ideal > cur_level + 0.7 or ideal < cur_level - 0.7:
        want = _forgiving_round(
            ideal
        )  # banker's rounding, matching HA's forgiving_round
    else:
        want = cur_level
    return min(want, cap)


def test_macro_matches_original_inline_formula_non_sleep():
    for t in [x / 10 for x in range(680, 900)]:  # 68.0 .. 89.9 °F
        for cur_level in range(0, 10):
            for is_night in (False, True):
                assert _target(t, cur_level, is_night, False) == _inline_target(
                    t, cur_level, is_night
                ), f"drift at t={t} cur={cur_level} night={is_night}"


# fan_nudge_level: the Tap Dial fan-dial-mode step. Current level + delta, clamped to 0..9 (0 = off).
def _nudge(cur_level, delta):
    return int(render_macro(FAN, "fan_nudge_level", cur_level, delta))


def test_fan_nudge_steps_within_range():
    assert _nudge(3, 1) == 4
    assert _nudge(3, -1) == 2


def test_fan_nudge_clamps_at_zero():
    assert _nudge(0, -1) == 0  # already off, stays off
    assert _nudge(1, -1) == 0  # step down to off


def test_fan_nudge_clamps_at_max():
    assert _nudge(9, 1) == 9  # already max, stays
    assert _nudge(8, 1) == 9


def test_fan_nudge_stays_bounded_over_full_range():
    for cur in range(0, 10):
        for delta in (-1, 1):
            assert 0 <= _nudge(cur, delta) <= 9
