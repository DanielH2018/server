"""Roll one day of Traefik and CrowdSec counters up into `edge.counts_daily`, and check it.

Both export cumulative counters that restart from zero with their pod. A day's count for a
series is its rise from its last value before the day began, counting a drop as a restart
(`prom_rollup.series_increase`). Every label stays in a series' identity: CrowdSec's agent
runs inside the Traefik pod, so a Traefik restart starts its counters under a new `instance`,
and a series the baseline does not hold counts from zero.

A row is a counter's daily total for one `name` and `detail`: Traefik's entrypoint or route
and the status code, or the CrowdSec scenario, AppSec engine or LAPI route. CrowdSec's active
decisions are a gauge, kept as the day's highest count per origin.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    window = DayWindow.for_day(day)
    for counter in COUNTERS:
        result = fetch_range(PROM_URL, counter.series, window, step_s=STEP_S,
                             through_midnight=True)
        baselines = baselines_by_series(fetch_instant(PROM_URL, baseline_query(counter, window)))
        rows += counter_rows(counter, window, result, baselines)
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .prom_rollup import DayWindow, series_identity, series_increase

SCHEMA = "edge"
COUNTS_TABLE = f"{SCHEMA}.counts_daily"
COUNTS_COLUMNS = ("day", "metric", "name", "detail", "value")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {COUNTS_TABLE} (day date NOT NULL, metric text NOT NULL, "
    "name text NOT NULL, detail text NOT NULL, value double precision NOT NULL, "
    "PRIMARY KEY (day, metric, name, detail))",
)

# A one-minute step: a reset loses the rise between the last sample before it and the reset.
STEP_S = 60
BASELINE_LOOKBACK = "1d"
BASELINE_SPAN = timedelta(days=1)
# Fewer requests than this means Traefik's counters were lost.
MIN_DAILY_REQUESTS = 1000

_CRD_SUFFIX = re.compile(r"-[0-9a-f]{20}@kubernetescrd$")


@dataclass(frozen=True)
class EdgeCounter:
    """One counter the warehouse keeps.

    Attributes:
      metric: The value of the `metric` column.
      series: The Prometheus counter.
      name_label: The label that fills the `name` column.
      max_daily: The most one day may sum to across every row, as a bound for `check`.
      detail_label: The label that fills the `detail` column, if any.
    """

    metric: str
    series: str
    name_label: str
    max_daily: float
    detail_label: str | None = None


COUNTERS = (
    EdgeCounter(
        "entrypoint_requests",
        "traefik_entrypoint_requests_total",
        "entrypoint",
        1e8,
        "code",
    ),
    EdgeCounter(
        "route_requests", "traefik_service_requests_total", "service", 1e8, "code"
    ),
    EdgeCounter("crowdsec_overflows", "cs_bucket_overflowed_total", "name", 1e7),
    EdgeCounter("appsec_requests", "cs_appsec_reqs_total", "appsec_engine", 1e8),
    EdgeCounter("appsec_blocks", "cs_appsec_block_total", "appsec_engine", 1e8),
    EdgeCounter("bouncer_requests", "cs_lapi_bouncer_requests_total", "route", 1e8),
)

DECISIONS_METRIC = "active_decisions_max"
DECISIONS_MAX = 1e7


def baseline_query(counter: EdgeCounter, window: DayWindow) -> str:
    """PromQL, as an instant query, for each series' last value before the day began."""
    return f"last_over_time({counter.series}[{BASELINE_LOOKBACK}] @ {window.start})"


def decisions_query(window: DayWindow) -> str:
    """PromQL, as an instant query, for the day's highest active-decision count per origin."""
    end = window.start + 86400
    return f"max_over_time(sum by (origin) (cs_active_decisions)[1d:{STEP_S}s] @ {end})"


def route_name(service: str) -> str:
    """A Traefik service label without its provider and the hash a CRD route appends."""
    return _CRD_SUFFIX.sub("", service).split("@", 1)[0]


def counter_rows(
    counter: EdgeCounter,
    window: DayWindow,
    result: list[dict],
    baselines: Mapping[frozenset, float],
) -> list[tuple]:
    """One `counts_daily` row per name and detail with a nonzero rise.

    Args:
      counter: The counter `result` holds.
      window: The day.
      result: The counter's range result over the day, through the next midnight.
      baselines: `baselines_by_series` over `baseline_query`'s result. A series missing from
        it began within the day and counts from zero.
    """
    sums: dict[tuple[str, str], float] = {}
    for series in result:
        labels = series["metric"]
        name = labels.get(counter.name_label, "")
        if counter.name_label == "service":
            name = route_name(name)
        detail = labels.get(counter.detail_label, "") if counter.detail_label else ""
        rise = series_increase(series, baselines.get(series_identity(series), 0.0))
        sums[(name, detail)] = sums.get((name, detail), 0.0) + rise
    return [
        (window.day, counter.metric, name, detail, round(value, 3))
        for (name, detail), value in sorted(sums.items())
        if value > 0
    ]


def decision_rows(window: DayWindow, result: list[dict]) -> list[tuple]:
    """One `counts_daily` row per decision origin, from `decisions_query`'s result."""
    return sorted(
        (
            window.day,
            DECISIONS_METRIC,
            series["metric"].get("origin", ""),
            "",
            float(series["value"][1]),
        )
        for series in result
    )


def quality_problems(
    day: date, rows: Sequence[Mapping], counters: Sequence[EdgeCounter] = COUNTERS
) -> list[str]:
    """Every reason the day's `counts_daily` rows are not trustworthy; empty when they are.

    Traefik serves monitoring probes all day and the bouncer polls CrowdSec's LAPI all day, so
    a day without either means its counters were lost, not that the edge was idle.
    """
    bounds = {counter.metric: counter.max_daily for counter in counters}
    bounds[DECISIONS_METRIC] = DECISIONS_MAX
    totals = dict.fromkeys(bounds, 0.0)
    problems = []
    for row in rows:
        if row["value"] < 0:
            problems.append(f"{day}: {row['metric']} has a negative row")
        totals[row["metric"]] = totals.get(row["metric"], 0.0) + row["value"]
    for metric, bound in bounds.items():
        if totals[metric] > bound:
            problems.append(f"{day}: {metric} sums to {totals[metric]}, over {bound}")
    if totals["entrypoint_requests"] < MIN_DAILY_REQUESTS:
        problems.append(
            f"{day}: Traefik served {totals['entrypoint_requests']:.0f} requests, "
            f"under {MIN_DAILY_REQUESTS}"
        )
    if totals["bouncer_requests"] == 0:
        problems.append(f"{day}: the bouncer made no LAPI requests")
    return problems
