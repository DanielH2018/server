"""Guard for the wake-fallback branch's color-tracker arming.

`bedroom_color_track` (files/automations/lighting.yaml) only drifts the bedroom color while it
believes the light is still in AUTO, which it decides by comparing the live color against
`input_number.bedroom_light_expected_color_temp`. Every other auto writer arms that tracker with the
color it just set — `bedroom_set_natural_brightness` does it inline. `bedroom_lights_set`'s
`wake_fallback` branch writes a fixed warm color directly, so it has to arm the tracker itself.

Tracking is suppressed for the whole ramp window by `in_wake_window`, so a missing arm is invisible
during the ramp and bites after it: tracking resumes against whatever a prior natural/wake/presence
call left in the tracker, which reads as "manual" and parks color tracking for the rest of the day.

The assertion is value-agnostic — it pins the two literals to EACH OTHER, not to 2200 — so a
deliberate retune of the fallback's warmth passes as long as both move together.
"""

from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parent.parent / "files"

TRACKER = "input_number.bedroom_light_expected_color_temp"


def _wake_fallback_branch() -> list:
    scripts = yaml.safe_load((FILES / "scripts" / "lighting.yaml").read_text())
    choose = next(
        step for step in scripts["bedroom_lights_set"]["sequence"] if "choose" in step
    )
    branch = next(
        b for b in choose["choose"] if "wake_fallback" in str(b.get("conditions"))
    )
    return branch["sequence"]


def test_wake_fallback_arms_the_color_tracker_at_the_color_it_sets():
    steps = _wake_fallback_branch()

    turn_on = next(s for s in steps if s.get("service") == "light.turn_on")
    set_value = next(
        (
            s
            for s in steps
            if s.get("service") == "input_number.set_value"
            and s["target"]["entity_id"] == TRACKER
        ),
        None,
    )
    assert set_value is not None, (
        f"bedroom_lights_set's wake_fallback branch no longer arms {TRACKER}; "
        "bedroom_color_track will compare its fixed warm color against a stale expected value"
    )
    assert int(set_value["data"]["value"]) == int(
        turn_on["data"]["color_temp_kelvin"]
    ), (
        "the wake_fallback branch arms the color tracker at a different color than it sets"
    )
