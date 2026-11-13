"""Invariant: a script that leaves the bedroom lights on at a FIXED color arms the color tracker.

`bedroom_color_track` (files/automations/lighting.yaml) only drifts the bedroom color while it
believes the light is still in AUTO, which it decides by comparing the live color against
`input_number.bedroom_light_expected_color_temp`. `bedroom_set_natural_brightness` arms that
tracker inline, so every caller that goes through it is covered. A script that writes a LITERAL
`color_temp_kelvin` instead bypasses that helper and has to arm the tracker itself.

Tracking is suppressed for the whole wake window by `in_wake_window`, so a missing arm is invisible
during a ramp and bites after it: tracking resumes against whatever a prior natural/wake/presence
call left in the tracker, which reads as "manual" and parks color tracking for the rest of the day.

Two shapes are out of scope, and both drop out on their own structure rather than by name:

* a sequence whose last light action is `light.turn_off` — `bedroom_blip` flashes warm and goes
  straight back off, and `bedroom_color_track` cannot run against lights that are off;
* a templated `color_temp_kelvin` — that is the `al_ct` read, which is the helper's own shape.

`bedroom_preview_wake` is the one named exemption: it is test-only (driven solely by
`script.bedroom_run_scenario`) and deliberately does not touch production tracker state.

The assertion is value-agnostic — it pins the tracker literal to the color literal, not to 2200 —
so a deliberate retune of a ramp's warmth passes as long as both move together.
"""

from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parent.parent / "files"

TRACKER = "input_number.bedroom_light_expected_color_temp"
LIGHTS = "light.bedroom_lights"

# Test-only writers, exempt from the invariant. Keep this list as short as the reason is specific.
ALLOWED_UNARMED = frozenset({"bedroom_preview_wake"})

# Non-vacuity: the scan must still find these two scripts. A YAML reshape that empties the subject
# set would otherwise pass an `all(...)` over nothing.
MUST_SCAN = frozenset({"bedroom_apply_wake", "bedroom_lights_set", "bedroom_blip"})

SEQUENCE_KEYS = ("sequence", "action", "then", "else", "default")


def _entity_ids(step: dict) -> list:
    target = step.get("target") or {}
    entity = target.get("entity_id", [])
    return [entity] if isinstance(entity, str) else list(entity)


def _sequences(node) -> list:
    """Every step-list reachable under a script, innermost blocks included."""
    found = []
    if isinstance(node, list):
        if any(isinstance(step, dict) for step in node):
            found.append(node)
        for step in node:
            found.extend(_sequences(step))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in SEQUENCE_KEYS or isinstance(value, (dict, list)):
                found.extend(_sequences(value))
    return found


def unarmed_fixed_color_writes(sequence: list) -> list:
    """The literal colors this step-list leaves on the lights without arming the tracker."""
    steps = [step for step in sequence if isinstance(step, dict)]
    light_calls = [
        step
        for step in steps
        if step.get("service") in ("light.turn_on", "light.turn_off")
        and LIGHTS in _entity_ids(step)
    ]
    if light_calls and light_calls[-1].get("service") == "light.turn_off":
        return []

    armed = {
        str(step.get("data", {}).get("value"))
        for step in steps
        if step.get("service") == "input_number.set_value"
        and TRACKER in _entity_ids(step)
    }
    unarmed = []
    for step in light_calls:
        color = step.get("data", {}).get("color_temp_kelvin")
        if isinstance(color, int) and str(color) not in armed:
            unarmed.append(color)
    return unarmed


def _scripts() -> dict:
    scripts = {}
    for path in sorted((FILES / "scripts").glob("*.yaml")):
        scripts.update(yaml.safe_load(path.read_text()) or {})
    return scripts


def test_the_scan_still_sees_the_scripts_this_invariant_exists_for():
    scanned = {
        name
        for name, body in _scripts().items()
        if any(
            step.get("service") in ("light.turn_on", "light.turn_off")
            and LIGHTS in _entity_ids(step)
            for sequence in _sequences(body)
            for step in sequence
            if isinstance(step, dict)
        )
    }
    assert MUST_SCAN <= scanned, (
        f"the bedroom-light scan no longer reaches {sorted(MUST_SCAN - scanned)}; "
        "the invariant below would pass vacuously"
    )


def test_every_fixed_color_write_arms_the_color_tracker():
    offenders = {}
    for name, body in _scripts().items():
        if name in ALLOWED_UNARMED:
            continue
        for sequence in _sequences(body):
            unarmed = unarmed_fixed_color_writes(sequence)
            if unarmed:
                offenders.setdefault(name, []).extend(unarmed)
    assert not offenders, (
        f"these scripts leave the bedroom lights at a fixed color without arming {TRACKER}: "
        f"{offenders}; bedroom_color_track will compare the live color against a stale expected "
        "value and park for the rest of the day"
    )


def test_a_fixed_color_write_without_the_arm_is_flagged():
    unarmed = unarmed_fixed_color_writes(
        [
            {
                "service": "light.turn_on",
                "target": {"entity_id": LIGHTS},
                "data": {"brightness_pct": 40, "color_temp_kelvin": 2200},
            }
        ]
    )
    assert unarmed == [2200]


def test_arming_at_a_different_color_than_it_sets_is_flagged():
    unarmed = unarmed_fixed_color_writes(
        [
            {
                "service": "light.turn_on",
                "target": {"entity_id": LIGHTS},
                "data": {"color_temp_kelvin": 2200},
            },
            {
                "service": "input_number.set_value",
                "target": {"entity_id": TRACKER},
                "data": {"value": 2700},
            },
        ]
    )
    assert unarmed == [2200]
