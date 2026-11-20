"""The Home Assistant rollup's transform and quality check, against a recorded day.

`fixtures/ha-2026-11-18.json` is `/api/history/period` for five of `SENSORS` over 2026-11-18
UTC, with `minimal_response` and `no_attributes`, as `fetch_history` asks for it.
"""

import io
import json
import re
import urllib.error
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

from dags.homelab_warehouse import ha_rollup as ha
from dags.homelab_warehouse import prom_rollup as pr

DAY = date(2026, 11, 18)
FIXTURES = Path(__file__).parent / "fixtures"
RECORDED = (
    "sensor.aqara_fp300_temperature",
    "sensor.outdoor_pm2_5",
    "sensor.behind_bed_power",
    "sensor.air_purifier_energy",
    "sensor.behind_bed_energy",
)
HA_CONFIG = Path(__file__).parents[2] / "home-assistant/files/configuration.yaml"


def sensors(*entity_ids: str) -> list[ha.Sensor]:
    by_id = {sensor.entity_id: sensor for sensor in ha.SENSORS}
    return [by_id[entity_id] for entity_id in entity_ids]


def recorded_rows() -> dict[str, dict]:
    history = json.loads((FIXTURES / "ha-2026-11-18.json").read_text())
    rows = ha.sensor_rows(DAY, history, sensors(*RECORDED))
    return {row[1]: dict(zip(ha.SENSOR_COLUMNS, row, strict=True)) for row in rows}


def state(value: str, at: str) -> dict:
    return {"entity_id": "sensor.x", "state": value, "last_changed": at}


def test_a_recorded_day_yields_a_row_per_sensor_and_passes_the_check():
    rows = recorded_rows()

    assert set(rows) == set(RECORDED)
    plug_power, plug_energy = (
        rows["sensor.behind_bed_power"],
        rows["sensor.behind_bed_energy"],
    )
    # A 17.3 W mean over 24 hours is 0.416 kWh, which the plug's own counter agrees with.
    assert plug_power["avg"] == 17.336
    assert plug_energy["increase"] == 0.41
    assert plug_power["unavailable_s"] == 16
    assert ha.quality_problems(DAY, list(rows.values()), sensors(*RECORDED)) == []


def test_the_mean_is_weighted_by_how_long_each_value_held():
    states = [
        state("10", "2026-11-18T00:00:00+00:00"),
        state("30", "2026-11-18T18:00:00+00:00"),
    ]

    row = ha.sensor_row(ha.Sensor("sensor.x", (0.0, 100.0)), DAY, states)

    assert row is not None
    assert row[3:6] == (10.0, 15.0, 30.0)


def test_unavailable_time_is_counted_and_a_reset_counter_counts_up_from_zero():
    states = [
        state("5", "2026-11-18T00:00:00+00:00"),
        state("7", "2026-11-18T06:00:00+00:00"),
        state("unavailable", "2026-11-18T07:00:00+00:00"),
        state("1", "2026-11-18T08:00:00+00:00"),
        state("2", "2026-11-18T12:00:00+00:00"),
    ]

    row = ha.sensor_row(ha.Sensor("sensor.x", ha.KWH, total=True), DAY, states)

    assert row is not None
    assert row[6] == 4.0
    assert row[8] == 3600


def test_a_missing_sensor_a_long_outage_and_a_value_out_of_bounds_each_fail_the_check():
    rows = recorded_rows()
    del rows["sensor.outdoor_pm2_5"]
    rows["sensor.behind_bed_power"]["unavailable_s"] = 5 * 3600
    rows["sensor.aqara_fp300_temperature"]["max"] = 140.0

    assert ha.quality_problems(DAY, list(rows.values()), sensors(*RECORDED)) == [
        f"{DAY}: sensor.aqara_fp300_temperature "
        f"{rows['sensor.aqara_fp300_temperature']['min']}-140.0 outside [30.0, 110.0]",
        f"{DAY}: no row for sensor.outdoor_pm2_5",
        f"{DAY}: sensor.behind_bed_power was unavailable for 300 minutes",
    ]


def test_a_day_is_loaded_only_once_complete_and_while_the_recorder_holds_all_of_it():
    now = datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc)

    assert ha.in_backfill_window(date(2026, 11, 18), now)
    assert ha.in_backfill_window(date(2026, 11, 13), now)
    assert not ha.in_backfill_window(date(2026, 11, 12), now)
    assert not ha.in_backfill_window(date(2026, 11, 19), now)


def test_the_backfill_window_matches_the_recorders_purge_setting():
    configured = re.search(r"^\s*purge_keep_days:\s*(\d+)", HA_CONFIG.read_text(), re.M)

    assert configured is not None
    assert int(configured.group(1)) == ha.HA_KEEP_DAYS


def test_a_refused_connection_is_retried_and_each_request_carries_the_token():
    requests = []
    sleeps = []

    def opener(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.URLError(
                ConnectionRefusedError(111, "Connection refused")
            )
        return io.BytesIO(b"[]")

    history = ha.fetch_history(
        "http://ha",
        "secret-token",
        DAY,
        ["sensor.x"],
        opener=opener,
        sleep=sleeps.append,
    )

    assert history == []
    assert len(requests) == 2
    assert sleeps == [pr.CONNECT_BACKOFF_S]
    assert requests[1].get_header("Authorization") == "Bearer secret-token"


def test_an_http_error_is_not_retried():
    calls = []
    with urllib.error.HTTPError(
        "http://ha", 401, "Unauthorized", Message(), io.BytesIO()
    ) as error:

        def opener(request, timeout):
            calls.append(request)
            raise error

        with pytest.raises(urllib.error.HTTPError):
            ha.fetch_history("http://ha", "bad", DAY, ["sensor.x"], opener=opener)

    assert len(calls) == 1
