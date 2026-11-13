"""bedroom_tap_dial_control admits exactly the actions a branch handles (#8387).

An action no branch handles still takes one of the automation's 10 queue slots, so a dial burst's
brightness_step_* and non-release button events pushed real dial steps out of the queue.
"""

import re
from pathlib import Path

import pytest
import yaml
from jinja_harness import render_template

LIGHTING = (
    Path(__file__).resolve().parent.parent / "files" / "automations" / "lighting.yaml"
)


def _tap_dial() -> dict:
    automations = yaml.safe_load(LIGHTING.read_text())
    return next(a for a in automations if a.get("id") == "bedroom_tap_dial_control")


def _branch_actions() -> list[str]:
    branches = _tap_dial()["action"][1]["choose"]
    found = []
    for branch in branches:
        found += re.findall(r"act == '([a-z0-9_]+)'", branch["conditions"])
    return found


def _admits(payload) -> bool:
    template = _tap_dial()["condition"][0]["value_template"]
    trigger = {} if payload is None else {"payload_json": payload}
    return render_template(template, trigger=trigger) == "True"


def test_every_button_branch_action_is_admitted():
    actions = _branch_actions()
    assert "button_4_hold_release" in actions
    for action in actions:
        assert _admits({"action": action}), action


def test_dial_rotations_are_admitted():
    assert _admits({"action": "dial_rotate_right_step"})


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "brightness_step_up"},
        {"action": "brightness_step_down"},
        {"action": "button_1_press"},
        {"action": "button_3_hold"},
        {"action": ""},
        {"battery": 80},
        None,
    ],
)
def test_unhandled_payloads_are_dropped(payload):
    assert not _admits(payload)


def test_fan_dial_starts_the_nudge_without_waiting():
    nudges = []
    for branch in _tap_dial()["action"][1]["choose"]:
        for step in branch["sequence"]:
            for inner in step.get("then", []):
                if "bedroom_fan_nudge" in str(inner):
                    nudges.append(inner)
    assert len(nudges) == 2
    for step in nudges:
        assert step["service"] == "script.turn_on"
        assert step["target"]["entity_id"] == "script.bedroom_fan_nudge"
