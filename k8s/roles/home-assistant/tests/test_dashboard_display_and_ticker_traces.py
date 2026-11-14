"""The bedroom dashboard, the display re-cast trigger and the wake tickers' trace depth.

The dashboard reads the T1's illuminance, because the lights dominate the FP300's (#8388). The
display's idle trigger holds for a minute, so a Cast socket reconnect does not re-cast (#8204),
and show holds off after a dismiss, so lux noise around both thresholds cannot flap it (#8411).
The per-minute wake tickers keep enough traces to cover a wake window (#8388).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from jinja_harness import render_template

ROLE = Path(__file__).resolve().parent.parent
FILES = ROLE / "files"


def _load(path: Path):
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load(path.read_text(), Loader=_Loader)


def _automation(filename: str, automation_id: str) -> dict:
    for automation in _load(FILES / "automations" / filename):
        if automation["id"] == automation_id:
            return automation
    raise AssertionError(f"{automation_id} not in {filename}")


def _dashboard_entities(node) -> set[str]:
    if isinstance(node, dict):
        found = {node["entity"]} if isinstance(node.get("entity"), str) else set()
        for value in node.values():
            found |= _dashboard_entities(value)
        return found
    if isinstance(node, list):
        return set().union(*(_dashboard_entities(item) for item in node))
    return set()


def test_dashboard_shows_t1_illuminance_and_the_newer_devices():
    entities = _dashboard_entities(_load(FILES / "ui-lovelace.yaml"))
    assert "sensor.aqara_fp300_illuminance" not in entities
    assert {
        "sensor.aqara_t1_illuminance",
        "binary_sensor.bedroom_auto_light_allowed",
        "switch.air_purifier",
        "binary_sensor.window_sensor_contact",
        "sensor.aqara_fp300_battery",
        "sensor.0x001788010f0ccda4_battery",
        "sensor.aqara_t1_battery",
        "sensor.window_sensor_battery",
    } <= entities


def test_display_show_idle_trigger_holds_for_a_minute():
    triggers = _automation("display.yaml", "bedroom_display_show")["trigger"]
    display = [
        t
        for t in triggers
        if t.get("platform") == "state"
        and t.get("entity_id") == "media_player.bedroom_display"
    ]
    assert len(display) == 1
    assert display[0].get("for") == "00:01:00"


NOW = datetime(2026, 11, 12, 19, 25, tzinfo=timezone.utc)


def _show_allowed_after_dismiss(dismissed: datetime | None, lux: str = "70") -> bool:
    conditions = _automation("display.yaml", "bedroom_display_show")["condition"]
    templates = [
        c["value_template"]
        for c in conditions
        if c.get("condition") == "template"
        and "automation.bedroom_display_dismiss" in c["value_template"]
    ]
    assert len(templates) == 1, templates
    attributes = {("automation.bedroom_display_dismiss", "last_triggered"): dismissed}
    rendered = render_template(
        templates[0],
        state_attr=lambda entity_id, name: attributes[(entity_id, name)],
        states=lambda entity_id: {"sensor.aqara_fp300_illuminance": lux}[entity_id],
        now=lambda: NOW,
        timedelta=timedelta,
    )
    return rendered == "True"


@pytest.mark.parametrize(("minutes_ago", "allowed"), [(14, False), (16, True)])
def test_display_show_holds_off_after_a_dismiss(minutes_ago, allowed):
    assert _show_allowed_after_dismiss(NOW - timedelta(minutes=minutes_ago)) is allowed


def test_display_show_casts_when_dismiss_never_ran():
    assert _show_allowed_after_dismiss(None)


def test_display_show_casts_inside_the_hold_once_the_lights_come_on():
    assert _show_allowed_after_dismiss(NOW - timedelta(minutes=1), lux="628")


def test_per_minute_wake_tickers_keep_90_traces():
    for automation_id in ("bedroom_wake_ramp", "bedroom_fallback_wake"):
        automation = _automation("wake-and-sleep.yaml", automation_id)
        assert automation.get("trace", {}).get("stored_traces") == 90, automation_id
