"""The Prometheus rollup's transforms and quality check, against a recorded day.

`fixtures/prom-2026-11-17-*.json` are the three range queries' `result` lists for 2026-11-17
UTC, recorded from the live Prometheus with `prom_rollup.fetch_range`.
"""

import copy
import io
import json
import urllib.error
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

from dags.homelab_warehouse import prom_rollup as pr

DAY = date(2026, 11, 17)
WINDOW = pr.DayWindow.for_day(DAY)
FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"prom-2026-11-17-{name}.json").read_text())


def as_dicts(rows: list[tuple], columns: tuple[str, ...]) -> list[dict]:
    return [dict(zip(columns, row, strict=True)) for row in rows]


def rolled_up(temps: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    ups = pr.ups_energy_rows(WINDOW, recorded("ups-load"), recorded("ups-nominal"))
    cpu = pr.cpu_temp_rows(WINDOW, temps if temps is not None else recorded("cpu-temp"))
    return as_dicts(ups, pr.UPS_COLUMNS), as_dicts(cpu, pr.TEMP_COLUMNS)


def test_a_recorded_day_yields_one_ups_row_and_one_row_per_host_and_passes_the_check():
    ups, cpu = rolled_up()

    assert [row["ups"] for row in ups] == ["nut-exporter.homelab.svc:9199"]
    assert {row["host"] for row in cpu} == {"daniel-box", "daniel-server"}
    # 17.238% mean load of a 900 W UPS for 24 hours.
    assert ups[0]["kwh"] == 3.7235
    assert all(r["samples"] == WINDOW.steps and r["max_gap_s"] == 0 for r in ups + cpu)
    assert all(r["min_c"] <= r["p95_c"] <= r["max_c"] for r in cpu)
    assert pr.quality_problems(DAY, ups, cpu) == []


def test_an_hour_of_missing_samples_reads_as_an_hour_gap_and_fails_the_check():
    temps = copy.deepcopy(recorded("cpu-temp"))
    temps[0]["values"] = temps[0]["values"][:300] + temps[0]["values"][360:]

    ups, cpu = rolled_up(temps)

    gappy = next(r for r in cpu if r["host"] == temps[0]["metric"]["instance"])
    assert gappy["max_gap_s"] == 3600
    assert pr.quality_problems(DAY, ups, cpu) == [
        f"{DAY}: host {gappy['host']} has a 60-minute gap"
    ]


def test_a_host_with_no_series_fails_the_check():
    ups, cpu = rolled_up(recorded("cpu-temp")[:1])

    (missing,) = pr.EXPECTED_HOSTS - {r["host"] for r in cpu}
    assert pr.quality_problems(DAY, ups, cpu) == [f"{DAY}: no row for host {missing}"]


def test_a_temperature_out_of_bounds_fails_the_check():
    ups, cpu = rolled_up()
    cpu[0]["max_c"] = 140.0

    assert pr.quality_problems(DAY, ups, cpu) == [
        f"{DAY}: host {cpu[0]['host']} temperature {cpu[0]['min_c']}-140.0 C outside [5.0, 110.0]"
    ]


def test_a_day_is_loaded_only_once_complete_and_only_while_prometheus_holds_all_of_it():
    oldest = datetime(2026, 10, 28, 3, tzinfo=timezone.utc)
    now = datetime(2026, 11, 19, 1, tzinfo=timezone.utc)

    assert not pr.in_backfill_window(date(2026, 10, 28), now, oldest)
    assert pr.in_backfill_window(date(2026, 10, 29), now, oldest)
    assert pr.in_backfill_window(date(2026, 11, 18), now, oldest)
    assert not pr.in_backfill_window(date(2026, 11, 19), now, oldest)


def test_max_gap_counts_missing_steps_at_both_ends_of_the_day():
    timestamps = range(WINDOW.start + 600, WINDOW.end - 120 + 1, pr.STEP_S)

    assert pr.max_gap_s(timestamps, WINDOW) == 600
    assert pr.max_gap_s([], WINDOW) == 86400


def _opener(outcomes: list):
    calls = []

    def opener(url, timeout):
        calls.append(url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(json.dumps(outcome).encode())

    return opener, calls


def test_a_refused_connection_is_retried_until_prometheus_answers():
    answer = {"status": "success", "data": {"result": recorded("ups-load")}}
    opener, _ = _opener(
        [urllib.error.URLError("refused"), urllib.error.URLError("refused"), answer]
    )
    slept = []

    result = pr.fetch_range(
        pr.PROM_URL, pr.UPS_LOAD_QUERY, WINDOW, opener, slept.append
    )

    assert result == recorded("ups-load")
    assert slept == [pr.CONNECT_BACKOFF_S, 2 * pr.CONNECT_BACKOFF_S]


def test_an_http_error_is_not_retried():
    with urllib.error.HTTPError(
        pr.PROM_URL, 400, "bad query", Message(), io.BytesIO()
    ) as error:
        opener, calls = _opener([error, error])

        with pytest.raises(urllib.error.HTTPError):
            pr.fetch_range(pr.PROM_URL, "not promql(", WINDOW, opener, lambda s: None)
    assert len(calls) == 1
