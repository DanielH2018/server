"""The weekday fallback wake starts whatever bedroom_manual_off holds, and only its ramp honours it.

START is the branch that clears manual_off (through script.bedroom_clear_overrides), so a
top-level manual_off gate made a night that ended with manual_off on a night with no wake at all
(#8381). A mid-ramp "off" still has to stop RAMP and RELEASE.
"""

from pathlib import Path

import yaml

AUTOMATIONS = (
    Path(__file__).resolve().parent.parent
    / "files"
    / "automations"
    / "wake-and-sleep.yaml"
)
MANUAL_OFF = "input_boolean.bedroom_manual_off"


def _fallback_wake() -> dict:
    automations = yaml.safe_load(AUTOMATIONS.read_text())
    found = [a for a in automations if a.get("id") == "bedroom_fallback_wake"]
    assert found, "bedroom_fallback_wake is gone from %s" % AUTOMATIONS.name
    return found[0]


def _branches() -> dict[str, dict]:
    choose = next(s for s in _fallback_wake()["action"] if "choose" in s)["choose"]
    by_name = {}
    for branch in choose:
        conditions = " ".join(str(c) for c in branch["conditions"])
        if "in_start_window" in conditions:
            by_name["start"] = branch
        elif "release_due" in conditions:
            by_name["release"] = branch
        elif "in_window" in conditions:
            by_name["ramp"] = branch
    assert set(by_name) == {"start", "ramp", "release"}, by_name
    return by_name


def test_manual_off_does_not_gate_the_whole_automation():
    conditions = str(_fallback_wake()["condition"])
    assert MANUAL_OFF not in conditions, conditions


def test_start_runs_with_manual_off_on_and_clears_it():
    start = _branches()["start"]
    assert MANUAL_OFF not in str(start["conditions"]), start["conditions"]
    services = [step.get("service") for step in start["sequence"]]
    assert "script.bedroom_clear_overrides" in services, services


def test_ramp_and_release_stop_on_manual_off():
    branches = _branches()
    for name in ("ramp", "release"):
        conditions = branches[name]["conditions"]
        assert any(MANUAL_OFF in str(c) and "'off'" in str(c) for c in conditions), (
            name,
            conditions,
        )
