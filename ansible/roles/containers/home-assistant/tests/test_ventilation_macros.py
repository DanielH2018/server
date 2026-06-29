"""Unit tests for the ventilation advisor macro in custom_templates/ventilation.jinja."""
from jinja_harness import render_macro

VENT = "ventilation.jinja"


def _advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale):
    return render_macro(VENT, "ventilation_advice",
                        indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale)


def test_smoke_guard_blocks_when_outdoor_pm_unsafe():
    # Stale + comfortable, but outdoor PM2.5 over the safe cap -> never advise ventilating.
    assert _advice(80, 65, 5, 50, True) == "none"


def test_small_outdoor_excess_within_margin_does_not_block():
    # Purifier keeps indoor very low (4); outdoor (12) is higher but within pm_dirty_margin (10) and
    # clean+comfortable -> still advise. (Was wrongly 'none' before the margin — the bare op>ip term
    # vetoed CO2/cooling ventilation whenever the purifier scrubbed indoor below outdoor.)
    assert _advice(75, 65, 4, 12, True) == "stale"


def test_large_outdoor_excess_over_indoor_still_blocks():
    # Outdoor (20) exceeds indoor (5) by more than the 10 margin -> still block (would worsen indoor).
    assert _advice(75, 65, 5, 20, True) == "none"


def test_dirty_margin_boundary_is_strict():
    # At exactly ip + margin it's allowed (strict >); one above blocks. (ip=5, margin=10 -> 15;
    # both 15 and 16 are at/above pm_relative_floor=15, so the relative term is in play here.)
    assert _advice(75, 65, 5, 15, True) == "stale"   # 15 == 5 + 10, not > -> allowed
    assert _advice(75, 65, 5, 16, True) == "none"    # 16 > 15 (margin) and 16 > 15 (floor) -> blocked


def test_safe_outdoor_above_scrubbed_indoor_still_ventilates():
    # Real 2026-06-29 incident: HEPA purifier scrubs indoor to ~0.5, outdoor 12 is objectively
    # safe (well under pm_safe and below pm_relative_floor=15) yet > ip + margin (0.5 + 10 = 10.5).
    # The floor gate must keep it advising instead of vetoing all CO2/free-cooling advice;
    # without pm_relative_floor this regresses to 'none'.
    assert _advice(75, 65, 0.5, 12, True) == "stale"


def test_relative_floor_gates_the_dirtier_than_indoor_veto():
    # indoor scrubbed to 1 so the relative term (op > ip + 10 = 11) is true from op=12 up.
    # At/below the floor (15) the veto is suppressed; above it the veto applies.
    assert _advice(75, 65, 1, 15, True) == "stale"   # 15 not > floor 15 -> relative veto suppressed
    assert _advice(75, 65, 1, 16, True) == "none"    # 16 > floor 15 and 16 > 11 -> blocked


def test_smoke_guard_blocks_when_outdoor_pm10_unsafe():
    # PM2.5 is clean + comfortable, but coarse PM10 (dust/pollen) is over its cap -> never
    # advise ventilating. Baseline (no PM10) for these inputs is 'stale'.
    assert _advice(75, 65, 8, 6, True) == "stale"   # baseline without PM10
    assert render_macro(VENT, "ventilation_advice", 75, 65, 8, 6, True,
                        outdoor_pm10=80) == "none"


def test_pm10_under_cap_does_not_block():
    # PM10 present but under the 50 cap -> still advises (stale air, clean PM2.5).
    assert render_macro(VENT, "ventilation_advice", 75, 65, 8, 6, True,
                        outdoor_pm10=40) == "stale"


def test_stale_air_when_clean_and_comfortable():
    assert _advice(75, 65, 8, 6, True) == "stale"


def test_stale_blocked_when_too_cold_outside():
    assert _advice(75, 40, 8, 6, True) == "none"


def test_stale_blocked_when_too_hot_outside():
    assert _advice(75, 90, 8, 6, True) == "none"


def test_free_cooling_when_warm_inside_and_cooler_clean_outside():
    assert _advice(82, 70, 8, 6, False) == "cool"


def test_cooling_needs_minimum_delta():
    assert _advice(80, 77, 8, 6, False) == "none"   # only 3°F cooler (< 5)


def test_cooling_needs_indoor_above_comfort():
    assert _advice(77, 60, 8, 6, False) == "none"   # 77 not > comfort_hi 78


def test_stale_outranks_cool():
    # Both stale and a cooling opportunity apply -> stale wins.
    assert _advice(82, 70, 8, 6, True) == "stale"


def test_comfort_band_edges_are_inclusive():
    assert _advice(75, 55, 8, 6, True) == "stale"   # lower edge
    assert _advice(75, 78, 8, 6, True) == "stale"   # upper edge
    assert _advice(75, 79, 8, 6, True) == "none"    # just past upper edge


def test_pm_safe_boundary():
    # ip high so the "dirtier than indoor" guard doesn't mask the cap test.
    assert _advice(75, 65, 30, 25, True) == "stale"  # 25 is not > 25 (cap is strict >)
    assert _advice(75, 65, 30, 26, True) == "none"   # 26 > 25 cap
