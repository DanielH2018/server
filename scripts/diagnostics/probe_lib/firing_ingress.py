"""`probe.py monitors`' second read: the firing ingress-probe alerts.

blackbox-exporter probes the ingress names since M4's PR 4 (#5625) removed Kuma's ingress tiles,
so Kuma's rollup no longer sees an ingress outage. These alerts answer for it until M6 Phase B
replaces the subcommand. The query carries no `shadow` filter: a shadow alert pages nowhere,
and this is where it is still read.
"""

import json

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path; pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch, as in `monitors`.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import prom_query_url
from lib.exit_codes import PROBE_UNREADABLE

QUERY = 'ALERTS{alertstate="firing",alertname=~"IngressProbe.*|BlackboxExporterDown"}'


def query_url(base):
    return prom_query_url(base, QUERY)


def format_ingress_alerts(data):
    """Format the firing ingress-probe alerts into one line each. Pure.

    `data` is the parsed instant-query response to `QUERY`, or a string saying why it could
    not be read. Returns (lines, exit_code).

    An empty result is clean here, unlike `monitor_status`'s: no series is what Prometheus
    returns when no alert fires. A response that is not `success`, or a read that failed,
    prints a line saying so and exits PROBE_UNREADABLE, so it never reads as clean.
    """
    if isinstance(data, str):
        return [f"  ingress probe alerts unread: {data}"], PROBE_UNREADABLE
    if data.get("status") != "success":
        detail = data.get("error") or f"status {data.get('status')!r}"
        return [f"  ingress probe alerts unread: {detail}"], PROBE_UNREADABLE
    lines = []
    for series in data.get("data", {}).get("result", []):
        labels = series.get("metric") or {}
        lines.append(
            f"  {labels.get('service', '?')}: {labels.get('alertname', '?')} firing"
        )
    return sorted(lines), 1 if lines else 0


def read(base, pin):
    """The parsed `QUERY` response, or a string saying why it went unread."""
    try:
        body = core.fetch(query_url(base), resolve=pin)
    except core.Unreadable as exc:
        return exc.message
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return "prometheus returned non-JSON"


def report(base, pin, kuma_code):
    """Print the firing ingress alerts, and fold their exit code into Kuma's rollup's.

    Either source's problem exits 1. Otherwise either unreadable source exits
    PROBE_UNREADABLE, and only two clean reads exit 0.
    """
    lines, code = format_ingress_alerts(read(base, pin))
    if lines:
        out = _sys.stderr if code == PROBE_UNREADABLE else _sys.stdout
        print("\n".join(["ingress probe alerts:", *lines]), file=out)
    if 1 in (kuma_code, code):
        return 1
    return max(kuma_code, code)
