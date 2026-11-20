"""Roll one day of Claude Code usage counters up into `claude.usage_daily`, and check it.

Claude Code exports cumulative counters, one series per session, so a session's use within a
day is how far its counter rose from where it last stood before the day began. Three things
make that harder than PromQL's `increase()`, which gets all three wrong:

- A restarted collector pod re-exports every live session under a new `instance`, so series
  are merged `without (instance)` into one counter per session.
- The collector forgets a series `metric_expiration` (168h) after its last update, and a
  restart forgets every idle one. A session that resumes later reappears with its whole
  lifetime total, so its baseline is its last value in the `BASELINE_LOOKBACK` before the day,
  not its value at midnight. A day is loaded only once Prometheus holds that much before it.
- A counter that drops was restarted by its own process and counts up again from zero.

Rows are summed by node, entrypoint, model and the counter's own type label, so no session
id, account or email reaches the warehouse.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    window = DayWindow.for_day(day)
    result = fetch_range(PROM_URL, usage_query(metric, window), window, step_s=STEP_S,
                         through_midnight=True)
    baselines = baselines_by_series(fetch_instant(PROM_URL, baseline_query(metric, window)))
    rows = usage_rows(metric, window, result, baselines)
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .prom_rollup import DayWindow

SCHEMA = "claude"
USAGE_TABLE = f"{SCHEMA}.usage_daily"
USAGE_COLUMNS = ("day", "metric", "node", "entrypoint", "model", "kind", "value")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (day date NOT NULL, metric text NOT NULL, "
    "node text NOT NULL, entrypoint text NOT NULL, model text NOT NULL, kind text NOT NULL, "
    "value double precision NOT NULL, "
    "PRIMARY KEY (day, metric, node, entrypoint, model, kind))",
)

# A five-minute step: a counter's value at the day's end is what a row needs, and the steps
# between only catch a restart.
STEP_S = 300
# A scrape gap shorter than this keeps a session's series continuous across it.
LOOKBACK = "1h"
# Longer than the collector's metric_expiration (168h), so a session the collector forgot
# still has its last value here.
BASELINE_LOOKBACK = "8d"
BASELINE_SPAN = timedelta(days=8)


@dataclass(frozen=True)
class UsageMetric:
    """One Claude Code counter the warehouse keeps.

    Attributes:
      metric: The value of the `metric` column.
      series: The Prometheus counter.
      max_daily: The most one day may sum to across every row, as a bound for `check`.
      kind_label: The counter's own label that splits it further, such as tokens' `type`.
    """

    metric: str
    series: str
    max_daily: float
    kind_label: str | None = None


METRICS = (
    UsageMetric("cost_usd", "claude_code_cost_usage_USD_total", 100_000.0),
    UsageMetric("tokens", "claude_code_token_usage_tokens_total", 1e11, "type"),
    UsageMetric("sessions", "claude_code_session_count_total", 100_000.0),
    UsageMetric("active_time_s", "claude_code_active_time_seconds_total", 30 * 86400.0),
    UsageMetric("lines_of_code", "claude_code_lines_of_code_count_total", 1e7, "type"),
    UsageMetric("commits", "claude_code_commit_count_total", 10_000.0),
    UsageMetric("pull_requests", "claude_code_pull_request_count_total", 10_000.0),
)


def _merged(selector: str) -> str:
    return f"max without (instance) ({selector})"


def usage_query(metric: UsageMetric, window: DayWindow) -> str:
    """PromQL, as a range query over the day, for the sessions whose counter moved in it.

    A session counts when its counter changed during the day, or when it appeared in the day,
    since a session that exported once has no change to show.
    """
    start, end = window.start, window.start + 86400
    s = metric.series
    return (
        f"{_merged(f'last_over_time({s}[{LOOKBACK}])')} and "
        f"({_merged(f'changes({s}[1d] @ {end})')} > 0 "
        f"or ({_merged(f'{s} @ {end}')} unless {_merged(f'{s} @ {start}')}))"
    )


def baseline_query(metric: UsageMetric, window: DayWindow) -> str:
    """PromQL, as an instant query, for each session's last value before the day began."""
    return _merged(
        f"last_over_time({metric.series}[{BASELINE_LOOKBACK}] @ {window.start})"
    )


def _identity(series: Mapping) -> frozenset:
    return frozenset(series["metric"].items())


def baselines_by_series(result: list[dict]) -> dict[frozenset, float]:
    """`baseline_query`'s instant result, keyed by each series' label set."""
    return {_identity(series): float(series["value"][1]) for series in result}


def series_increase(series: Mapping, baseline: float) -> float:
    """One session counter's rise over the day from `baseline`, counting a restart from zero."""
    previous = baseline
    rise = 0.0
    for _, value in sorted((int(float(t)), float(v)) for t, v in series["values"]):
        rise += value - previous if value >= previous else value
        previous = value
    return rise


def usage_rows(
    metric: UsageMetric,
    window: DayWindow,
    result: list[dict],
    baselines: Mapping[frozenset, float],
) -> list[tuple]:
    """One `usage_daily` row per node, entrypoint, model and kind with a nonzero rise.

    Args:
      metric: The counter `result` holds.
      window: The day.
      result: `usage_query`'s range result.
      baselines: `baselines_by_series` over `baseline_query`'s result. A session missing
        from it began within the day and counts from zero.
    """
    sums: dict[tuple[str, str, str, str], float] = {}
    for series in result:
        labels = series["metric"]
        key = (
            labels.get("node", ""),
            labels.get("app_entrypoint", ""),
            labels.get("model", ""),
            labels.get(metric.kind_label, "") if metric.kind_label else "",
        )
        rise = series_increase(series, baselines.get(_identity(series), 0.0))
        sums[key] = sums.get(key, 0.0) + rise
    return [
        (window.day, metric.metric, *key, round(value, 6))
        for key, value in sorted(sums.items())
        if value > 0
    ]


def quality_problems(
    day: date, rows: Sequence[Mapping], metrics: Sequence[UsageMetric] = METRICS
) -> list[str]:
    """Every reason the day's `usage_daily` rows are not trustworthy; empty when they are.

    A day with no Claude Code use at all is valid, so no row is required. Spend and tokens
    come from the same requests, so one without the other means a series was lost.
    """
    totals = {metric.metric: 0.0 for metric in metrics}
    problems = []
    for row in rows:
        if row["value"] < 0:
            problems.append(f"{day}: {row['metric']} has a negative row")
        totals[row["metric"]] = totals.get(row["metric"], 0.0) + row["value"]
    for metric in metrics:
        if totals[metric.metric] > metric.max_daily:
            problems.append(
                f"{day}: {metric.metric} sums to {totals[metric.metric]}, "
                f"over {metric.max_daily}"
            )
    if (totals.get("cost_usd", 0) > 0) != (totals.get("tokens", 0) > 0):
        problems.append(f"{day}: cost and tokens disagree on whether Claude Code ran")
    return problems
