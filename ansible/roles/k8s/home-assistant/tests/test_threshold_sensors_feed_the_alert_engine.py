"""Every threshold sensor feeds both edges of bedroom_threshold_alert (#8386, #8382).

A threshold sensor missing from the engine's trigger lists is defined, computed and silent.
"""

from pathlib import Path

import yaml
from ha_state_model import slugify

FILES = Path(__file__).resolve().parent.parent / "files"


def _threshold_sensors() -> dict[str, str]:
    return {
        f"binary_sensor.{slugify(s['name'])}": s["entity_id"]
        for s in yaml.safe_load((FILES / "thresholds.yaml").read_text())
    }


def _engine_edges() -> dict[str, set[str]]:
    automations = yaml.safe_load((FILES / "automations" / "alerts.yaml").read_text())
    engine = next(a for a in automations if a["id"] == "bedroom_threshold_alert")
    edges: dict[str, set[str]] = {"bad": set(), "ok": set()}
    for trigger in engine["trigger"]:
        edges[trigger["id"].split("_")[1]] |= set(trigger["entity_id"])
    return edges


def test_every_threshold_sensor_triggers_both_edges():
    sensors = _threshold_sensors()
    edges = _engine_edges()
    for entity in sensors:
        assert entity in edges["bad"], entity
        assert entity in edges["ok"], entity


def test_battery_and_watch_sources_have_threshold_sensors():
    sources = set(_threshold_sensors().values())
    assert {
        "sensor.aqara_fp300_battery",
        "sensor.aqara_t1_battery",
        "sensor.window_sensor_battery",
        "sensor.pixel_watch_next_alarm_overdue",
    } <= sources
