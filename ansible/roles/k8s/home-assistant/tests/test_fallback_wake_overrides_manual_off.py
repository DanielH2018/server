"""The weekday fallback wake starts whatever bedroom_manual_off holds, and only its ramp honours it.

START is the branch that clears manual_off (through script.bedroom_clear_overrides), so a
top-level manual_off gate made a night that ended with manual_off on a night with no wake at all
(#8381). A mid-ramp "off" still has to stop RAMP and RELEASE.
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja_harness import render_template

AUTOMATIONS = (
    Path(__file__).resolve().parent.parent
    / "files"
    / "automations"
    / "wake-and-sleep.yaml"
)
MANUAL_OFF = "input_boolean.bedroom_manual_off"
FRIDAY_0610 = datetime(2026, 11, 13, 6, 10, tzinfo=timezone.utc)
# Every branch variable set so that only manual_off decides whether a branch runs.
BRANCH_VARIABLES = {
    "no_real_wake": True,
    "in_start_window": True,
    "wake_today": False,
    "in_window": True,
    "release_due": True,
}


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


def _holds(condition, manual_off: str) -> bool:
    states = {"person.daniel": "home", MANUAL_OFF: manual_off}
    if isinstance(condition, dict) and condition["condition"] == "state":
        return states[condition["entity_id"]] == condition["state"]
    template = condition if isinstance(condition, str) else condition["value_template"]
    rendered = render_template(
        template,
        is_state=lambda entity_id, state: states[entity_id] == state,
        now=lambda: FRIDAY_0610,
        **BRANCH_VARIABLES,
    )
    return rendered == "True"


def _runs(branch: str, manual_off: str) -> bool:
    conditions = _fallback_wake()["condition"] + _branches()[branch]["conditions"]
    return all(_holds(c, manual_off) for c in conditions)


def test_start_runs_with_manual_off_on_and_clears_it():
    assert _runs("start", manual_off="on")
    services = [step.get("service") for step in _branches()["start"]["sequence"]]
    assert "script.bedroom_clear_overrides" in services, services


def test_ramp_and_release_stop_on_manual_off():
    for branch in ("ramp", "release"):
        assert _runs(branch, manual_off="off"), branch
        assert not _runs(branch, manual_off="on"), branch
