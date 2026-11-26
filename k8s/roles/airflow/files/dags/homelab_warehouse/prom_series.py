"""Roll one day of host, GPU, UPS battery and volume series up into `prom.series_daily`.

Each `Rollup` names one warehouse metric: the PromQL that yields it, the labels that key its
rows, and what the day's `check` holds its rows to. Every metric lands in the one long table,
one row per (day, metric, key), so a new metric is a new `ROLLUPS` entry and no new DDL.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    window = DayWindow.for_day(day)
    rows = [row for r in ROLLUPS for row in series_rows(r, window, fetch_range(PROM_URL, r.query, window))]
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .prom_rollup import (
    EXPECTED_HOSTS,
    EXPECTED_UPS,
    MAX_GAP_S,
    SCHEMA,
    DayWindow,
    day_stats,
)

SERIES_TABLE = f"{SCHEMA}.series_daily"
SERIES_COLUMNS = (
    "day",
    "metric",
    "key",
    "min",
    "avg",
    "max",
    "p95",
    "samples",
    "max_gap_s",
)
SERIES_DDL = (
    f"CREATE TABLE IF NOT EXISTS {SERIES_TABLE} (day date NOT NULL, metric text NOT NULL, "
    'key text NOT NULL, "min" double precision NOT NULL, avg double precision NOT NULL, '
    '"max" double precision NOT NULL, p95 double precision NOT NULL, '
    "samples integer NOT NULL, max_gap_s integer NOT NULL, PRIMARY KEY (day, metric, key))",
)

NODE_HOSTS = EXPECTED_HOSTS | {"daniel-pi"}


@dataclass(frozen=True)
class Rollup:
    """One warehouse metric: how to read it, how to key it, and what its day must hold.

    Attributes:
      metric: The value of the `metric` column.
      query: PromQL whose series are the metric's keys.
      key_labels: The labels joined with `/` into the `key` column.
      expected: The keys every day must carry. None checks only `min_rows`.
      min_rows: The fewest rows a day may hold.
      gap_limit_s: A key's longest allowed gap. None checks no gap.
      bounds: The range every sample must fall in, inclusive.
    """

    metric: str
    query: str
    bounds: tuple[float, float]
    key_labels: tuple[str, ...] = ("instance",)
    expected: frozenset[str] | None = None
    min_rows: int = 1
    gap_limit_s: int | None = MAX_GAP_S


ROLLUPS = (
    Rollup(
        "node_load5",
        "max by (instance) (node_load5)",
        bounds=(0.0, 256.0),
        expected=NODE_HOSTS,
    ),
    Rollup(
        "node_mem_used_pct",
        "max by (instance) (100 * (1 - node_memory_MemAvailable_bytes"
        " / node_memory_MemTotal_bytes))",
        bounds=(0.0, 100.0),
        expected=NODE_HOSTS,
    ),
    # i915 exposes only an RC6 idle counter, whose complement is the busy fraction.
    Rollup(
        "gpu_busy_pct",
        "max by (instance) (homelab_gpu_busy_percent) or max by (instance) "
        "(clamp(100 * (1 - rate(homelab_gpu_rc6_residency_seconds_total[5m])), 0, 100))",
        bounds=(0.0, 100.0),
        expected=EXPECTED_HOSTS,
    ),
    Rollup(
        "ups_battery_charge_pct",
        "max by (instance) (network_ups_tools_battery_charge)",
        bounds=(0.0, 100.0),
        expected=EXPECTED_UPS,
    ),
    Rollup(
        "ups_battery_runtime_s",
        "max by (instance) (network_ups_tools_battery_runtime)",
        bounds=(0.0, 86400.0),
        expected=EXPECTED_UPS,
    ),
    # kubelet reports only claims a running pod mounts, so gaps are legitimate: no gap check, count
    # floor.
    Rollup(
        "volume_used_bytes",
        "max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes)",
        bounds=(0.0, 2.0**50),
        key_labels=("namespace", "persistentvolumeclaim"),
        min_rows=20,
        gap_limit_s=None,
    ),
    Rollup(
        "volume_used_pct",
        "max by (namespace, persistentvolumeclaim) (100 * kubelet_volume_stats_used_bytes"
        " / kubelet_volume_stats_capacity_bytes)",
        bounds=(0.0, 100.0),
        key_labels=("namespace", "persistentvolumeclaim"),
        min_rows=20,
        gap_limit_s=None,
    ),
)


def series_rows(rollup: Rollup, window: DayWindow, result: list[dict]) -> list[tuple]:
    """One `series_daily` row per series in `result`, keyed by `rollup.key_labels`."""
    rows = []
    for series in result:
        stats = day_stats(series, window)
        if stats is None:
            continue
        key = "/".join(series["metric"].get(label, "") for label in rollup.key_labels)
        rows.append(
            (
                window.day,
                rollup.metric,
                key,
                stats.min,
                stats.avg,
                stats.max,
                stats.p95,
                stats.samples,
                stats.max_gap_s,
            )
        )
    return rows


def series_problems(
    day: date, rows: Sequence[Mapping], rollups: Sequence[Rollup] = ROLLUPS
) -> list[str]:
    """Every reason the day's `series_daily` rows are not trustworthy; empty when they are.

    Args:
      day: The day checked.
      rows: The day's rows, read back from the warehouse.
      rollups: The metrics the day must hold.
    """
    problems = []
    for rollup in rollups:
        mine = [row for row in rows if row["metric"] == rollup.metric]
        name = rollup.metric
        if rollup.expected is not None:
            missing = rollup.expected - {row["key"] for row in mine}
            problems += [
                f"{day}: {name} has no row for {key}" for key in sorted(missing)
            ]
        elif len(mine) < rollup.min_rows:
            problems.append(
                f"{day}: {name} has {len(mine)} rows, fewer than {rollup.min_rows}"
            )
        low, high = rollup.bounds
        for row in mine:
            if (
                rollup.gap_limit_s is not None
                and row["max_gap_s"] >= rollup.gap_limit_s
            ):
                problems.append(
                    f"{day}: {name} {row['key']} has a {row['max_gap_s'] // 60}-minute gap"
                )
            if not low <= row["min"] <= row["max"] <= high:
                problems.append(
                    f"{day}: {name} {row['key']} {row['min']}-{row['max']} "
                    f"outside [{low}, {high}]"
                )
    return problems
