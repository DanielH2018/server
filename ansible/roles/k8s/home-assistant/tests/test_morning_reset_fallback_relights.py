"""Invariant: the 09:00 fallback reads the night's state before it clears it.

`bedroom_morning_reset` (files/automations/wake-and-sleep.yaml) fires on the real alarm and on a
09:00 fallback. The fallback hands the room back to automatic lighting when the night ran all the
way to 09:00 — without it the bedtime nightlight (RGB amber at 3%) holds the bedroom there for the
rest of the day, because colour tracking needs colour-temp mode, the presence branch is a noop
while the light is on, and only the wake-ramp release clears Adaptive Lighting's manual_control
(#2509).

Its discriminator is `input_boolean.bedroom_sleep_mode`, which `script.bedroom_clear_overrides`
turns off. HA evaluates a `variables:` step when it reaches it, so the capture has to sit BEFORE
that call. Move it after and the branch still parses, still deploys, and can never fire again —
a regression with no error and no log line, visible only on a no-alarm morning. That ordering is
what this file pins.
"""

from pathlib import Path

import yaml

AUTOMATIONS = (
    Path(__file__).resolve().parent.parent
    / "files"
    / "automations"
    / "wake-and-sleep.yaml"
)

CLEAR = "script.bedroom_clear_overrides"
WRITER = "script.bedroom_lights_set"


def _morning_reset() -> dict:
    automations = yaml.safe_load(AUTOMATIONS.read_text())
    reset = [a for a in automations if a.get("id") == "bedroom_morning_reset"]
    assert reset, "bedroom_morning_reset is gone from %s" % AUTOMATIONS.name
    return reset[0]


def _fallback_branch() -> dict:
    branches = [
        step
        for step in _morning_reset()["action"]
        if isinstance(step, dict) and "fallback" in str(step.get("if", ""))
    ]
    assert len(branches) == 1, branches
    return branches[0]


def test_the_night_state_is_captured_before_the_overrides_are_cleared():
    actions = _morning_reset()["action"]
    captures = [i for i, step in enumerate(actions) if "variables" in step]
    clears = [i for i, step in enumerate(actions) if step.get("service") == CLEAR]
    assert captures and clears, actions
    assert captures[0] < clears[0], (
        "the variables step must precede %s, which turns bedroom_sleep_mode off" % CLEAR
    )
    captured = actions[captures[0]]["variables"]
    assert "bedroom_sleep_mode" in captured["was_sleeping"], captured
    assert "light.bedroom_lights" in captured["light_on"], captured


def test_the_fallback_branch_relights_through_the_sanctioned_writer():
    branch = _fallback_branch()
    condition = branch["if"]
    assert "was_sleeping" in condition and "light_on" in condition, condition
    assert branch["then"] == [{"service": WRITER, "data": {"reason": "natural"}}], (
        branch["then"]
    )
