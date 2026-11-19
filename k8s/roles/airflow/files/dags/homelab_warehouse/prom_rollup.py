"""Roll one day of Prometheus series up into daily warehouse rows, and check them.

Two rollups: daily UPS energy from NUT, and daily CPU package temperature per host. Each reads
a range query at a one-minute step, so a missing step is a gap of at least a minute, and
`max_gap_s` on every row is the longest run of missing steps.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    window = DayWindow.for_day(day)
    temps = cpu_temp_rows(window, fetch_range(PROM_URL, CPU_TEMP_QUERY, window))
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

PROM_URL = "http://prometheus.observability.svc.cluster.local:9090"
STEP_S = 60

# Prometheus' 7GB size cap binds before its 30-day time cap, so the oldest day it still holds
# is read live from this series rather than assumed.
OLDEST_SAMPLE_QUERY = "prometheus_tsdb_lowest_timestamp_seconds"

UPS_LOAD_QUERY = "network_ups_tools_ups_load"
UPS_NOMINAL_QUERY = "network_ups_tools_ups_realpower_nominal"
# temp1 is the package sensor on both chips: coretemp on daniel-server, k10temp on daniel-box.
CPU_TEMP_QUERY = (
    'max by (instance) (node_hwmon_temp_celsius{chip=~"platform_coretemp_0|'
    'pci0000:00_0000:00:18_3",sensor="temp1"})'
)

SCHEMA = "prom"
UPS_TABLE = f"{SCHEMA}.ups_energy_daily"
UPS_COLUMNS = ("day", "ups", "kwh", "avg_load_pct", "nominal_w", "samples", "max_gap_s")
TEMP_TABLE = f"{SCHEMA}.cpu_temp_daily"
TEMP_COLUMNS = ("day", "host", "min_c", "max_c", "p95_c", "samples", "max_gap_s")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {UPS_TABLE} (day date NOT NULL, ups text NOT NULL, "
    "kwh double precision NOT NULL, avg_load_pct double precision NOT NULL, "
    "nominal_w double precision NOT NULL, samples integer NOT NULL, "
    "max_gap_s integer NOT NULL, PRIMARY KEY (day, ups))",
    f"CREATE TABLE IF NOT EXISTS {TEMP_TABLE} (day date NOT NULL, host text NOT NULL, "
    "min_c double precision NOT NULL, max_c double precision NOT NULL, "
    "p95_c double precision NOT NULL, samples integer NOT NULL, "
    "max_gap_s integer NOT NULL, PRIMARY KEY (day, host))",
)

# The keys every day must carry a row for.
EXPECTED_UPS = frozenset({"nut-exporter.homelab.svc:9199"})
EXPECTED_HOSTS = frozenset({"daniel-box", "daniel-server"})

# A gap this long means the source or the scrape was down for a real part of the day.
MAX_GAP_S = 3600
TEMP_BOUNDS_C = (5.0, 110.0)

# kube-router admits a new pod's traffic a few seconds after the pod starts (ADR 0009), and a
# KubernetesExecutor task pod's first call lands inside that window.
CONNECT_ATTEMPTS = 6
CONNECT_BACKOFF_S = 5.0


@dataclass(frozen=True)
class DayWindow:
    """One UTC day, as the step timestamps a range query returns for it.

    Attributes:
      day: The day.
      start: Unix seconds of the day's first step, 00:00.
      end: Unix seconds of the day's last step, 23:59.
    """

    day: date
    start: int
    end: int

    @classmethod
    def for_day(cls, day: date) -> "DayWindow":
        start = int(
            datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
        )
        return cls(day, start, start + 86400 - STEP_S)

    @property
    def steps(self) -> int:
        return (self.end - self.start) // STEP_S + 1


def in_backfill_window(day: date, now: datetime, oldest_sample: datetime) -> bool:
    """Whether `day` is complete and Prometheus still holds all of it.

    Args:
      day: The day a run would load.
      now: The current time.
      oldest_sample: The time of Prometheus' oldest stored sample. A day starting before it is
        already partly dropped, and loading it would fail the quality check on a gap.
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return oldest_sample <= day_start and day_start + timedelta(days=1) <= now


def oldest_sample(
    base_url: str,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> datetime:
    """The time of Prometheus' oldest stored sample.

    Raises:
      RuntimeError: Prometheus did not answer with exactly one sample.
    """
    params = urllib.parse.urlencode({"query": OLDEST_SAMPLE_QUERY})
    result = _get(f"{base_url}/api/v1/query?{params}", opener, sleep)["result"]
    if len(result) != 1:
        raise RuntimeError(
            f"{OLDEST_SAMPLE_QUERY} returned {len(result)} series, not 1"
        )
    return datetime.fromtimestamp(float(result[0]["value"][1]), timezone.utc)


def _get(url: str, opener: Callable, sleep: Callable[[float], None]) -> dict:
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            with opener(url, timeout=60) as response:
                body = json.load(response)
            break
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            sleep(CONNECT_BACKOFF_S * (attempt + 1))
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus refused {url}: {body.get('error', body)}")
    return body["data"]


def fetch_range(
    base_url: str,
    query: str,
    window: DayWindow,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Run a Prometheus range query over `window` and return its `result` list.

    Retries a refused or unreachable connection `CONNECT_ATTEMPTS` times. An HTTP error or a
    non-success body is not retried: the query, not the network, is wrong.

    Raises:
      RuntimeError: Prometheus answered with a non-success status.
      urllib.error.URLError: The connection failed on every attempt.
    """
    params = urllib.parse.urlencode(
        {"query": query, "start": window.start, "end": window.end, "step": STEP_S}
    )
    return _get(f"{base_url}/api/v1/query_range?{params}", opener, sleep)["result"]


def _points(series: Mapping) -> dict[int, float]:
    return {int(float(ts)): float(value) for ts, value in series["values"]}


def max_gap_s(timestamps: Iterable[int], window: DayWindow) -> int:
    """The longest run of missing steps in `window`, in seconds; the whole day when empty."""
    present = sorted(t for t in timestamps if window.start <= t <= window.end)
    if not present:
        return window.steps * STEP_S
    edges = [window.start - STEP_S, *present, window.end + STEP_S]
    return max(b - a - STEP_S for a, b in zip(edges, edges[1:], strict=False))


def percentile(values: list[float], q: float) -> float:
    """The `q` quantile of `values` by linear interpolation, as PromQL's quantile computes it."""
    ordered = sorted(values)
    rank = q * (len(ordered) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def ups_energy_rows(
    window: DayWindow, load: list[dict], nominal: list[dict]
) -> list[tuple]:
    """One row per UPS: the day's energy from its load percentage and its nominal real power.

    kWh is the mean power over the samples present times 24 hours, so a short gap does not
    read as lower consumption; `max_gap_s` says how much the mean had to cover.
    """
    nominal_by_ups = {s["metric"].get("instance", ""): _points(s) for s in nominal}
    rows = []
    for series in load:
        ups = series["metric"].get("instance", "")
        points = _points(series)
        watts_at = nominal_by_ups.get(ups, {})
        paired = {t: pct for t, pct in points.items() if t in watts_at}
        if not paired:
            continue
        powers = [pct / 100.0 * watts_at[t] for t, pct in paired.items()]
        mean_w = sum(powers) / len(powers)
        rows.append(
            (
                window.day,
                ups,
                round(mean_w * 24 / 1000, 4),
                round(sum(paired.values()) / len(paired), 3),
                max(watts_at[t] for t in paired),
                len(paired),
                max_gap_s(paired, window),
            )
        )
    return rows


@dataclass(frozen=True)
class DayStats:
    """One series' summary over one day, from the steps the range query returned."""

    min: float
    avg: float
    max: float
    p95: float
    samples: int
    max_gap_s: int


def day_stats(series: Mapping, window: DayWindow) -> DayStats | None:
    """The day's summary of one range-query series; None when it has no sample in `window`."""
    points = {
        t: v for t, v in _points(series).items() if window.start <= t <= window.end
    }
    if not points:
        return None
    values = list(points.values())
    return DayStats(
        min=min(values),
        avg=round(sum(values) / len(values), 3),
        max=max(values),
        p95=round(percentile(values, 0.95), 3),
        samples=len(values),
        max_gap_s=max_gap_s(points, window),
    )


def cpu_temp_rows(window: DayWindow, temps: list[dict]) -> list[tuple]:
    """One row per host: the day's min, max and 95th percentile package temperature."""
    rows = []
    for series in temps:
        stats = day_stats(series, window)
        if stats is None:
            continue
        rows.append(
            (
                window.day,
                series["metric"].get("instance", ""),
                stats.min,
                stats.max,
                stats.p95,
                stats.samples,
                stats.max_gap_s,
            )
        )
    return rows


def quality_problems(
    day: date, ups_rows: Sequence[Mapping], temp_rows: Sequence[Mapping]
) -> list[str]:
    """Every reason the day's loaded rows are not trustworthy; empty when they are.

    Args:
      day: The day checked.
      ups_rows: The day's `ups_energy_daily` rows, read back from the warehouse.
      temp_rows: The day's `cpu_temp_daily` rows, read back from the warehouse.
    """
    problems = []
    for kind, rows, key, expected in (
        ("ups", ups_rows, "ups", EXPECTED_UPS),
        ("host", temp_rows, "host", EXPECTED_HOSTS),
    ):
        missing = expected - {row[key] for row in rows}
        problems += [f"{day}: no row for {kind} {name}" for name in sorted(missing)]
        for row in rows:
            if row["max_gap_s"] >= MAX_GAP_S:
                problems.append(
                    f"{day}: {kind} {row[key]} has a {row['max_gap_s'] // 60}-minute gap"
                )
    for row in ups_rows:
        ceiling = row["nominal_w"] * 24 / 1000
        if not 0 < row["kwh"] <= ceiling:
            problems.append(
                f"{day}: ups {row['ups']} kWh {row['kwh']} outside (0, {ceiling}]"
            )
    low, high = TEMP_BOUNDS_C
    for row in temp_rows:
        if not low <= row["min_c"] <= row["max_c"] <= high:
            problems.append(
                f"{day}: host {row['host']} temperature {row['min_c']}-{row['max_c']} C "
                f"outside [{low}, {high}]"
            )
    return problems
