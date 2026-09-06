"""`probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import json
import re
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import (
    loki_endpoint,
    loki_query_url,
    prom_endpoint,
    prom_query_url,
    since_window_ns,
)


# --- Range-window truncation ---------------------------------------------------------------
#
# Prometheus here retains ~11.5 days, so a `[30d]` range selector returns eleven days of
# samples with no error, no warning and no partial-data marker (issue #1314). A derivation
# over that window then quotes a denominator three times the covered one. `metric` therefore
# measures what Prometheus actually holds and says so when the query asks for more.

# A range selector or a subquery: `[30d]`, `[1h30m]`, `[500ms]`, `[30d:1h]`. The optional
# `:<resolution>` tail is the subquery step, which does not widen the window.
_RANGE_SELECTOR = re.compile(r"\[\s*((?:\d+(?:ms|[smhdwy]))+)\s*(?::[^\]]*)?\]")
_DURATION_COMPONENT = re.compile(r"(\d+)(ms|[smhdwy])")
_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    "y": 365 * 86400,
}

# The retention floor as one instant query through `/api/v1/query`. The prometheus IngressRoute
# admits only `/api/v1/query` and `/api/v1/targets`, so `/api/v1/status/tsdb` is unreachable.
# `time()` is Prometheus' own clock, which keeps the answer free of this host's clock skew.
_SPAN_PROMQL = "time() - min(prometheus_tsdb_lowest_timestamp_seconds)"

# An empty TSDB reports a max-int sentinel as its lowest timestamp, so the subtraction lands
# hugely negative; a bad parse can land absurdly high. Neither is a retention span.
_MAX_PLAUSIBLE_SPAN_S = 10 * 365 * 86400

# Retention sized exactly to the window still falls a scrape or a compaction short of it.
# Only a shortfall past 1% is the defect this warns about.
_SHORTFALL_SLACK = 0.99


def requested_range_seconds(promql):
    """The longest range-selector window in `promql`, in seconds — None when it has none.

    The longest, because a query holding several selectors is truncated as soon as its
    widest one outruns retention.
    """
    spans = []
    for match in _RANGE_SELECTOR.finditer(promql):
        spans.append(
            sum(
                int(count) * _UNIT_SECONDS[unit]
                for count, unit in _DURATION_COMPONENT.findall(match.group(1))
            )
        )
    return max(spans) if spans else None


def covered_span_seconds(base, pin):
    """Seconds of history this Prometheus holds, or None when that cannot be read.

    Every failure returns None, so a warning that cannot be computed stays silent rather than
    failing the `metric` command it annotates.
    """
    try:
        data = json.loads(core.fetch(prom_query_url(base, _SPAN_PROMQL), resolve=pin))
        result = ((data.get("data") or {}).get("result")) or []
        span = float(result[0]["value"][1])
    except SystemExit, OSError, ValueError, KeyError, IndexError, TypeError:
        return None
    if not 0 < span < _MAX_PLAUSIBLE_SPAN_S:
        return None
    return span


def format_span(seconds):
    """A retention span as `11.5d` / `3.2h` / `45m`, matching how a selector is written."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{seconds:.0f}s"


def truncation_warning(promql, base, pin):
    """The one-line warning for a range query Prometheus cannot cover, else None."""
    requested = requested_range_seconds(promql)
    if requested is None:
        return None
    covered = covered_span_seconds(base, pin)
    if covered is None or covered >= requested * _SHORTFALL_SLACK:
        return None
    return (
        f"warning: covered span {format_span(covered)} of {format_span(requested)} "
        "requested — Prometheus retains less than this query asks for, so the window is "
        "truncated and any rate or ratio over it has the shorter denominator"
    )


def format_metric(data):
    """Human view of a Prometheus /api/v1/query result.

    One `<labels> = <value>` line per series (labels are the metric dict minus __name__); a single
    label-less series prints just the value, so scalars read cleanly. A matrix (range vector) shows
    each series' latest point. Empty result -> 'no data'.

    Replaces the recurring `… | python3 -c "…[print(r['metric'].get('X'),'=', r['value'][1]) …]"`
    reshapes.
    """
    d = data.get("data") or {}
    result = d.get("result") or []
    if d.get("resultType") == "scalar":  # result = [ts, "val"]
        return str(result[1]) if len(result) == 2 else "no data"
    if not result:
        return "no data"
    lines = []
    for series in result:
        labels = {
            k: v for k, v in (series.get("metric") or {}).items() if k != "__name__"
        }
        key = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        if "value" in series:  # instant vector
            val = series["value"][1]
        else:  # matrix -> latest point
            vals = series.get("values") or []
            val = vals[-1][1] if vals else "?"
        lines.append(f"{key} = {val}" if key else str(val))
    return "\n".join(lines)


def format_loki(data):
    """Human view of a Loki query_range result: just the log lines.

    Sorted oldest -> newest across all streams (nanosecond-epoch timestamps), so the newest sits
    nearest the prompt. An empty result renders 'no logs'.

    Replaces the recurring `… | python3 -c "…for v in r['values']: print(v[1])"`.
    """
    rows = []
    for stream in (data.get("data") or {}).get("result") or []:
        for ts, line in stream.get("values") or []:
            rows.append((int(ts), line))
    if not rows:
        return "no logs"
    rows.sort(key=lambda r: r[0])
    return "\n".join(line for _, line in rows)


def run_query(ns):
    """Fetch a metric / loki-query and print the formatted view (the default).

    `--json` and `--dry-run` never reach here — they take the raw streaming path.
    """
    if ns.cmd == "metric":
        base, pin = prom_endpoint()
        url = prom_query_url(base, ns.promql)
        formatter = format_metric
    else:
        base, pin = loki_endpoint()
        # `metric` shares this function and its subparser declares no --since, so read the
        # attribute defensively. No `direction`: Loki's default `backward` is what makes
        # --limit return the NEWEST N lines, which format_loki then sorts oldest-first.
        # run_alerts' `direction=forward` is for episode reconstruction and does not belong here.
        start, end = since_window_ns(getattr(ns, "since", None))
        url = loki_query_url(base, ns.logql, ns.limit, start=start, end=end)
        formatter = format_loki
    data, err = core.fetch_json(url, resolve=pin)
    if err:
        return err
    if ns.cmd == "metric":
        # stderr, so a caller piping the value is unaffected while the reader still sees it.
        warning = truncation_warning(ns.promql, base, pin)
        if warning:
            print(warning, file=_sys.stderr)
    print(formatter(data))
    return 0
