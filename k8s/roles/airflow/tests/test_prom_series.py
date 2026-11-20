"""The `series_daily` rollups and their check, against a recorded day.

`fixtures/prom-2026-11-17-<metric>.json` are each `ROLLUPS` query's `result` list for
2026-11-17 UTC, recorded from the live Prometheus with `prom_rollup.fetch_range`. The two
volume fixtures keep two claims of the 41 the day held.
"""

import copy
import json
from datetime import date
from pathlib import Path

from dags.homelab_warehouse import prom_rollup as pr
from dags.homelab_warehouse import prom_series as ps

DAY = date(2026, 11, 17)
WINDOW = pr.DayWindow.for_day(DAY)
FIXTURES = Path(__file__).parent / "fixtures"
BY_METRIC = {rollup.metric: rollup for rollup in ps.ROLLUPS}


def recorded(metric: str) -> list[dict]:
    name = metric.replace("_", "-")
    return json.loads((FIXTURES / f"prom-2026-11-17-{name}.json").read_text())


def rows_for(metric: str, result: list[dict] | None = None) -> list[dict]:
    rollup = BY_METRIC[metric]
    rows = ps.series_rows(
        rollup, WINDOW, result if result is not None else recorded(metric)
    )
    return [dict(zip(ps.SERIES_COLUMNS, row, strict=True)) for row in rows]


def recorded_day() -> list[dict]:
    return [row for rollup in ps.ROLLUPS for row in rows_for(rollup.metric)]


def check(rows: list[dict], *metrics: str) -> list[str]:
    return ps.series_problems(DAY, rows, [BY_METRIC[m] for m in metrics])


def test_a_recorded_day_keys_each_metric_as_its_rollup_says():
    keys = {}
    for row in recorded_day():
        keys.setdefault(row["metric"], set()).add(row["key"])

    assert keys["node_load5"] == {"daniel-box", "daniel-server", "daniel-pi"}
    # daniel-box's from the amdgpu gauge, daniel-server's from the i915 RC6 counter.
    assert keys["gpu_busy_pct"] == {"daniel-box", "daniel-server"}
    assert keys["ups_battery_runtime_s"] == {"nut-exporter.homelab.svc:9199"}
    assert keys["volume_used_bytes"] == {
        "homelab/authelia-config",
        "observability/prometheus-data",
    }


def test_a_recorded_day_passes_the_check_once_the_volume_floor_is_met():
    rows = recorded_day()
    without_volumes = [r for r in ps.ROLLUPS if not r.metric.startswith("volume_")]

    assert ps.series_problems(DAY, rows, without_volumes) == []
    assert check(rows, "volume_used_bytes") == [
        f"{DAY}: volume_used_bytes has 2 rows, fewer than 20"
    ]


def test_a_host_missing_from_a_keyed_metric_fails_the_check():
    rows = [r for r in rows_for("node_mem_used_pct") if r["key"] != "daniel-pi"]

    assert check(rows, "node_mem_used_pct") == [
        f"{DAY}: node_mem_used_pct has no row for daniel-pi"
    ]


def test_an_hour_gap_fails_a_host_metric_but_not_a_volume_metric():
    load = copy.deepcopy(recorded("node_load5"))
    load[0]["values"] = load[0]["values"][:300] + load[0]["values"][360:]
    volumes = copy.deepcopy(recorded("volume_used_pct"))
    volumes[0]["values"] = volumes[0]["values"][:300] + volumes[0]["values"][360:]
    gappy = load[0]["metric"]["instance"]

    assert check(rows_for("node_load5", load), "node_load5") == [
        f"{DAY}: node_load5 {gappy} has a 60-minute gap"
    ]
    assert check(rows_for("volume_used_pct", volumes) * 10, "volume_used_pct") == []


def test_a_value_out_of_bounds_fails_the_check():
    rows = rows_for("ups_battery_charge_pct")
    rows[0]["max"] = 101.0

    assert check(rows, "ups_battery_charge_pct") == [
        f"{DAY}: ups_battery_charge_pct nut-exporter.homelab.svc:9199 "
        f"{rows[0]['min']}-101.0 outside [0.0, 100.0]"
    ]
