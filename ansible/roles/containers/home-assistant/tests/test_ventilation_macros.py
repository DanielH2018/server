"""Unit tests for the ventilation advisor macro in custom_templates/ventilation.jinja."""

from jinja_harness import render_macro

VENT = "ventilation.jinja"


def _advice(indoor_temp, outdoor_temp, indoor_pm, outdoor_pm, air_stale):
    return render_macro(
        VENT,
        "ventilation_advice",
        indoor_temp,
        outdoor_temp,
        indoor_pm,
        outdoor_pm,
        air_stale,
    )


def test_smoke_guard_blocks_when_outdoor_pm_unsafe():
    # Stale + comfortable, but outdoor PM2.5 over the safe cap -> never advise ventilating.
    assert _advice(80, 65, 5, 50, True) == "none"


def test_scrubbed_indoor_does_not_veto_safe_outdoor():
    # A HEPA purifier keeps indoor PM very low (4); outdoor (12) is higher but under pm_safe and
    # clean+comfortable -> still advise. There is NO relative "dirtier than indoors" veto: the bare
    # op>ip term used to falsely block CO2/cooling ventilation whenever the purifier scrubbed indoor
    # below outdoor. The absolute pm_safe/pm10_safe caps are the only air-quality gate now.
    assert _advice(75, 65, 4, 12, True) == "stale"


def test_safe_but_moderate_outdoor_ventilates_over_scrubbed_indoor():
    # The recurring purifier regression (3rd fix): outdoor PM 18 is safe-but-moderate (< pm_safe 25)
    # while a scrubbed indoor sits at 2. The old relative floor (15) still vetoed the whole [15, 25)
    # band; with the relative term dropped, safe-but-moderate air now ventilates for both stale-air ...
    assert _advice(75, 65, 2, 18, True) == "stale"
    # ... and free-cooling advice (warm inside, cooler + safe outside).
    assert _advice(82, 70, 2, 18, False) == "cool"


def test_safe_outdoor_above_scrubbed_indoor_still_ventilates():
    # Real 2026-06-29 incident: HEPA purifier scrubs indoor to ~0.5, outdoor 12 is objectively safe
    # (well under pm_safe). Must keep advising instead of vetoing all CO2/free-cooling advice.
    assert _advice(75, 65, 0.5, 12, True) == "stale"


def test_smoke_guard_blocks_when_outdoor_pm10_unsafe():
    # PM2.5 is clean + comfortable, but coarse PM10 (dust/pollen) is over its cap -> never
    # advise ventilating. Baseline (no PM10) for these inputs is 'stale'.
    assert _advice(75, 65, 8, 6, True) == "stale"  # baseline without PM10
    assert (
        render_macro(VENT, "ventilation_advice", 75, 65, 8, 6, True, outdoor_pm10=80)
        == "none"
    )


def test_pm10_under_cap_does_not_block():
    # PM10 present but under the 50 cap -> still advises (stale air, clean PM2.5).
    assert (
        render_macro(VENT, "ventilation_advice", 75, 65, 8, 6, True, outdoor_pm10=40)
        == "stale"
    )


def test_stale_air_when_clean_and_comfortable():
    assert _advice(75, 65, 8, 6, True) == "stale"


def test_stale_blocked_when_too_cold_outside():
    assert _advice(75, 40, 8, 6, True) == "none"


def test_stale_blocked_when_too_hot_outside():
    assert _advice(75, 90, 8, 6, True) == "none"


def test_free_cooling_when_warm_inside_and_cooler_clean_outside():
    assert _advice(82, 70, 8, 6, False) == "cool"


def test_cooling_needs_minimum_delta():
    assert _advice(80, 77, 8, 6, False) == "none"  # only 3°F cooler (< 5)


def test_cooling_needs_indoor_above_comfort():
    assert _advice(77, 60, 8, 6, False) == "none"  # 77 not > comfort_hi 78


def test_stale_outranks_cool():
    # Both stale and a cooling opportunity apply -> stale wins.
    assert _advice(82, 70, 8, 6, True) == "stale"


def test_comfort_band_edges_are_inclusive():
    assert _advice(75, 55, 8, 6, True) == "stale"  # lower edge
    assert _advice(75, 78, 8, 6, True) == "stale"  # upper edge
    assert _advice(75, 79, 8, 6, True) == "none"  # just past upper edge


def test_pm_safe_boundary():
    # ip high so the "dirtier than indoor" guard doesn't mask the cap test.
    assert _advice(75, 65, 30, 25, True) == "stale"  # 25 is not > 25 (cap is strict >)
    assert _advice(75, 65, 30, 26, True) == "none"  # 26 > 25 cap
