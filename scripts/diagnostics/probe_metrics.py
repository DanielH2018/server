"""`probe.py metric` and `probe.py loki-query` -- Prometheus and Loki queries.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core
from probe_core import (
    loki_endpoint,
    loki_query_url,
    prom_endpoint,
    prom_query_url,
    since_window_ns,
)


def format_metric(data):
    """Human view of a Prometheus /api/v1/query result. One `<labels> = <value>`
    line per series (labels are the metric dict minus __name__); a single
    label-less series prints just the value, so scalars read cleanly. A matrix
    (range vector) shows each series' latest point. Empty result -> 'no data'.

    Replaces the recurring `… | python3 -c "…[print(r['metric'].get('X'),'=',
    r['value'][1]) …]"` reshapes."""
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
    """Human view of a Loki query_range result: just the log lines, sorted oldest
    -> newest across all streams (nanosecond-epoch timestamps), so the newest sits
    nearest the prompt. Empty result -> 'no logs'.

    Replaces the recurring `… | python3 -c "…for v in r['values']: print(v[1])"`."""
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
    `--json` and `--dry-run` never reach here — they take the raw streaming path."""
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
    print(formatter(data))
    return 0
