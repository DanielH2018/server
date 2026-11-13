"""The wake-window gates count a fallback-wake morning as a wake (#8383).

On a fallback morning sensor.bedroom_wake_start is unavailable, or dated tomorrow, and only
input_datetime.bedroom_last_wake marks the wake. A gate that built its window from wake_start alone
let bedroom_absence_off cut the fallback ramp and ended the purifier hold at 06:00.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from jinja_harness import render_template

FILES = Path(__file__).resolve().parent.parent / "files"
WINDOW_SENSOR = "binary_sensor.bedroom_in_wake_window"

NOW = datetime(2026, 11, 13, 6, 20, tzinfo=timezone.utc)


def _template_entity(unique_id: str) -> dict:
    for block in yaml.safe_load((FILES / "templates.yaml").read_text()):
        for entity in block.get("binary_sensor", []):
            if entity.get("unique_id") == unique_id:
                return entity
    raise AssertionError("no template binary_sensor %s" % unique_id)


def _automation(automation_id: str) -> dict:
    automations = yaml.safe_load((FILES / "automations" / "lighting.yaml").read_text())
    found = [a for a in automations if a.get("id") == automation_id]
    assert found, automation_id
    return found[0]


def _render(source: str, wake_start: str, last_wake: datetime | None) -> str:
    entity_states = {
        "sensor.bedroom_wake_start": wake_start,
        "input_boolean.bedroom_sleep_mode": "off",
    }

    def as_timestamp(value, default=None):
        try:
            return datetime.fromisoformat(value).timestamp()
        except TypeError, ValueError:
            return default

    def state_attr(entity_id, attribute):
        assert (entity_id, attribute) == (
            "input_datetime.bedroom_last_wake",
            "timestamp",
        )
        return last_wake.timestamp() if last_wake else None

    return render_template(
        source,
        states=lambda e: entity_states[e],
        is_state=lambda e, s: entity_states[e] == s,
        state_attr=state_attr,
        as_timestamp=as_timestamp,
        now=lambda: NOW,
    )


def test_the_window_sensor_is_on_during_a_fallback_ramp():
    state = _template_entity("bedroom_in_wake_window")["state"]
    fallback_start = NOW - timedelta(minutes=20)
    assert _render(state, "unavailable", fallback_start) == "True"
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    assert _render(state, tomorrow, fallback_start) == "True"
    assert _render(state, "unavailable", NOW - timedelta(days=1)) == "False"


def test_the_purifier_hold_spans_a_fallback_ramp():
    state = _template_entity("bedroom_purifier_hold")["state"]
    assert _render(state, "unavailable", NOW - timedelta(minutes=20)) == "True"
    assert _render(state, "unavailable", NOW - timedelta(minutes=50)) == "False"


def test_the_other_gates_read_the_window_sensor():
    gates = {
        "bedroom_auto_light_allowed": _template_entity("bedroom_auto_light_allowed"),
        "bedroom_absence_off": _automation("bedroom_absence_off")["condition"],
        "bedroom_al_startup_suppress": _automation("bedroom_al_startup_suppress")[
            "action"
        ],
        "bedroom_color_track": _automation("bedroom_color_track")["condition"],
    }
    for name, gate in gates.items():
        text = str(gate)
        assert WINDOW_SENSOR in text, name
        assert "sensor.bedroom_wake_start" not in text, name
