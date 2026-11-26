"""`probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

import json
import re
import sys as _sys


# Use `core.<name>` for anything tests monkeypatch: a `from core import` binds a snapshot the patch
# never reaches.
from diagnostics.probe_lib import core
from lib import prom_duration
from diagnostics.probe_lib.core import (
    loki_endpoint,
    loki_query_url,
    prom_endpoint,
    prom_query_url,
    since_window_ns,
    pick_loki_store,
)


# Prometheus retains ~11.5 days, so a `[30d]` selector silently returns eleven days; `metric`
# measures what is held and says so.

_RANGE_SELECTOR = re.compile(r"\[\s*((?:\d+(?:ms|[smhdwy]))+)\s*(?::[^\]]*)?\]")

# One instant query: the prometheus IngressRoute admits only `/api/v1/query` and `/api/v1/targets`.
# `time()` is Prometheus' own clock.
SPAN_PROMQL = "time() - min(prometheus_tsdb_lowest_timestamp_seconds)"

# An empty TSDB reports a max-int sentinel, so the subtraction lands hugely negative; neither that
# nor an absurdly high value is a span.
_MAX_PLAUSIBLE_SPAN_S = 10 * 365 * 86400

# Retention sized exactly to the window falls a scrape short; only a shortfall past 1% warns.
_SHORTFALL_SLACK = 0.99


def requested_range_seconds(promql):
    """The longest range-selector window in `promql`, in seconds — None when it has none.

    The longest, because a query holding several selectors is truncated as soon as its
    widest one outruns retention.
    """
    found = (
        prom_duration.seconds(m.group(1)) for m in _RANGE_SELECTOR.finditer(promql)
    )
    spans = [s for s in found if s is not None]
    return max(spans) if spans else None


def covered_span_seconds(base, pin):
    """Seconds of history this Prometheus holds, or None when that cannot be read.

    Every failure returns None, so a warning that cannot be computed stays silent rather than
    failing the `metric` command it annotates.
    """
    try:
        data = json.loads(core.fetch(prom_query_url(base, SPAN_PROMQL), resolve=pin))
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
    # Loki keeps pre-redaction `_handle_signal_show_view` records (cast refresh_token) for 744h;
    # `--json` streams raw and stays unredacted.
    return core.redact_log_secrets("\n".join(line for _, line in rows))


def run_query(ns):
    """Fetch a metric / loki-query and print the formatted view (the default).

    `--json` and `--dry-run` never reach here — they take the raw streaming path.
    """
    if ns.cmd == "metric":
        base, pin = prom_endpoint()
        url = prom_query_url(base, ns.promql)
        formatter = format_metric
    else:
        store, note = pick_loki_store(ns.logql, ns.loki)
        if note:
            print(note, file=_sys.stderr)
        base, pin = loki_endpoint(store)
        # `metric` shares this function and its subparser declares no --since, so read the
        # attribute defensively. No `direction`: Loki's default `backward` is what makes
        # --limit return the NEWEST N lines, which format_loki then sorts oldest-first.
        # run_alerts asks for the same end explicitly (`direction=backward`), since `forward`
        # spends a capped window's whole limit on its oldest lines.
        start, end = since_window_ns(
            getattr(ns, "since", None), getattr(ns, "until", None)
        )
        url = loki_query_url(
            base, ns.logql, ns.limit, start=start, end=end, step=ns.step
        )
        formatter = format_loki
    data, err = core.fetch_json(url, resolve=pin, timeout=getattr(ns, "timeout", None))
    if err:
        return err
    if ns.cmd == "metric":
        # stderr, so a caller piping the value is unaffected while the reader still sees it.
        warning = truncation_warning(ns.promql, base, pin)
        if warning:
            print(warning, file=_sys.stderr)
    print(formatter(data))
    return 0
