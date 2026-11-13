"""The battery and watch sources each have a threshold sensor (#8386, #8382).

`threshold_pairing_errors` (scripts/home_assistant/ha_state_checks.py) proves a declared threshold
is wired into the alert engine; it cannot tell a source was never declared.
"""

from pathlib import Path

import yaml

FILES = Path(__file__).resolve().parent.parent / "files"


def test_battery_and_watch_sources_have_threshold_sensors():
    sources = {
        s["entity_id"] for s in yaml.safe_load((FILES / "thresholds.yaml").read_text())
    }
    assert {
        "sensor.aqara_fp300_battery",
        "sensor.aqara_t1_battery",
        "sensor.window_sensor_battery",
        "sensor.pixel_watch_next_alarm_overdue",
    } <= sources
