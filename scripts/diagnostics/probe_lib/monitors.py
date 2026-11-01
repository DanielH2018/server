"""`probe.py monitors` -- what is down, read from the alert plane.

Four reads, in order:

1. `ALERTS{alertstate="firing"}` from Prometheus, rolled up by `severity` and `service`. The
   Watchdog alert is the plane's own heartbeat: rule evaluation always fires it, so a result
   without it proves nothing and the read exits PROBE_UNREADABLE (failure class 3).
2. The active alerts Alertmanager holds that `ALERTS` lacks, folded into the same rollup. The
   loki-homelab ruler sends its alerts to Alertmanager directly, so `ALERTS` never carries
   them (`HostOutOfMemoryKill`, `LokiPiLogsSilent`).
3. The Kuma tiles that are not up. Batches that have not cut over still page through Kuma,
   and their rules here are shadows, so this arm stays until M6's C-1 deletes the
   `uptime-kuma` scrape. An empty result is clean because `KumaDown` fires on a dead or
   absent scrape, and read 1 reports it.
4. Alertmanager's active silences (`alertmanager_api.report`).
"""

from collections import defaultdict

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path; pyproject's `pythonpath` is a pytest setting. This has to sit
# ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import alertmanager_api, core
from diagnostics.probe_lib.core import prom_endpoint, prom_query_url
from lib.exit_codes import PROBE_UNREADABLE

ALERTS_QUERY = 'ALERTS{alertstate="firing"}'
KUMA_QUERY = 'monitor_status{job="uptime-kuma"} != 1'
WATCHDOG = "Watchdog"
# The severities that make `monitors` exit 1. `info` alerts are listed as notes.
PAGING_SEVERITIES = ("critical", "warning")

# Kuma's own numeric status codes, from the exporter that feeds monitor_status.
_MONITOR_STATUS_LABELS = {"0": "DOWN", "2": "PENDING", "3": "MAINTENANCE"}


def _result(data, source):
    """(series list, None), or (None, a line saying why `data` is unusable). Pure."""
    if isinstance(data, str):
        return None, f"  {source} unread: {data}"
    if data.get("status") != "success":
        detail = data.get("error") or f"status {data.get('status')!r}"
        return None, f"  {source} unread: {detail}"
    return data.get("data", {}).get("result", []), None


def alertmanager_only(result, held):
    """The label sets of the alerts in `held` that no `ALERTS` series in `result` matches. Pure.

    `held` is Alertmanager's active-alert list. An alert matches a series with the same alert
    name, `service` and severity, which is how a Prometheus alert appears in both.
    """

    def key(labels):
        return tuple(labels.get(k) for k in ("alertname", "service", "severity"))

    seen = {key(s.get("metric") or {}) for s in result}
    return [
        a.get("labels") or {} for a in held if key(a.get("labels") or {}) not in seen
    ]


def format_alerts(data, held):
    """(lines, exit code) for the firing-alert rollup. Pure.

    `data` is the parsed response to `ALERTS_QUERY`, or a string saying why it went unread.
    `held` is Alertmanager's active-alert list, or a string saying why it went unread; its
    alerts that `ALERTS` lacks join the rollup marked `(Alertmanager only)`. A non-shadow `critical` or `warning` alert exits 1. Shadow alerts and `info` alerts are
    listed under their own headings and never change the code: a shadow rule is still under
    its soak, and Kuma pages for its batch. No Watchdog, or an unread response, exits
    PROBE_UNREADABLE unless a paging alert already makes it 1. So does an unread `held`.
    """
    result, unread = _result(data, "ALERTS")
    if unread:
        return [unread], PROBE_UNREADABLE
    extra = [] if isinstance(held, str) else alertmanager_only(result, held)
    sources = [(s.get("metric") or {}, "") for s in result]
    sources += [(labels, " (Alertmanager only)") for labels in extra]
    paging, notes, shadow = defaultdict(list), [], []
    watchdog = False
    for labels, origin in sources:
        if labels.get("alertname") == WATCHDOG:
            watchdog = watchdog or not origin
            continue
        service = labels.get("service", "?")
        severity = labels.get("severity", "?")
        name = labels.get("alertname", "?")
        line = f"  {service}: {name}{{severity={severity}}}{origin}"
        if labels.get("shadow") == "true":
            shadow.append(line)
        elif severity in PAGING_SEVERITIES:
            paging[severity].append(f"  {service}: {name}{origin}")
        else:
            notes.append(line)
    count = sum(len(v) for v in paging.values())
    lines = [f"{count} paging alert(s) firing"]
    for severity in PAGING_SEVERITIES:
        if paging[severity]:
            lines += [f"{severity}:", *sorted(paging[severity])]
    if notes:
        lines += ["notes (not paging):", *sorted(notes)]
    if shadow:
        lines += ["shadow rules firing (Kuma still pages their batch; not counted):"]
        lines += sorted(shadow)
    if not watchdog:
        lines.insert(
            0,
            f"{WATCHDOG} is not firing: Prometheus rule evaluation is down, so an alert "
            "missing from this list may still be failing",
        )
    if isinstance(held, str):
        lines.append(f"  Alertmanager alerts unread: {held}")
    clean = watchdog and not isinstance(held, str)
    return lines, 1 if count else (0 if clean else PROBE_UNREADABLE)


def format_kuma(data):
    """(lines, exit code) for the Kuma tiles that are not up. Pure.

    `data` is the parsed response to `KUMA_QUERY`, or a string saying why it went unread. Any
    series is a tile not up, so it exits 1; an unread response exits PROBE_UNREADABLE.
    """
    result, unread = _result(data, "Kuma monitor_status")
    if unread:
        return [unread], PROBE_UNREADABLE
    lines = []
    for series in result:
        name = (series.get("metric") or {}).get("monitor_name", "?")
        status = (series.get("value") or [None, None])[1]
        lines.append(f"  {name}: {_MONITOR_STATUS_LABELS.get(status, status)}")
    if not lines:
        return [], 0
    return ["Kuma tiles not up (batches not yet cut over):", *sorted(lines)], 1


def combine(*codes):
    """One exit code from several reads: any 1 wins, then any unread, then 0."""
    return 1 if 1 in codes else max(codes)


def _read(url, pin):
    """The parsed response at `url`, or a string saying why it went unread."""
    try:
        return core.fetch_parsed(url, resolve=pin)
    except core.Unreadable as exc:
        return exc.message


def _read_held(url, pin):
    """Alertmanager's active-alert list at `url`, or a string saying why it went unread."""
    try:
        return alertmanager_api.read(url, pin)
    except core.Unreadable as exc:
        return exc.message


def run_monitors(ns):
    """Print the firing-alert rollup, the Kuma tiles not up, then the active silences.

    Exit 0 = no paging alert fires, no Kuma tile is down, and every source was read.
    """
    base, pin = prom_endpoint()
    alerts_url = prom_query_url(base, ALERTS_QUERY)
    kuma_url = prom_query_url(base, KUMA_QUERY)
    am_base, am_pin = alertmanager_api.endpoint()
    held_url = alertmanager_api.active_alerts_url(am_base)
    if ns.dry_run:
        core.print_dry_run(alerts_url, resolve=pin)
        core.print_dry_run(held_url, resolve=am_pin)
        core.print_dry_run(kuma_url, resolve=pin)
        return core.print_dry_run(
            alertmanager_api.silences_url(am_base), resolve=am_pin
        )
    alert_lines, alert_code = format_alerts(
        _read(alerts_url, pin), _read_held(held_url, am_pin)
    )
    kuma_lines, kuma_code = format_kuma(_read(kuma_url, pin))
    code = combine(alert_code, kuma_code)
    for lines, part in ((alert_lines, alert_code), (kuma_lines, kuma_code)):
        if lines:
            out = _sys.stderr if part == PROBE_UNREADABLE else _sys.stdout
            print("\n".join(lines), file=out)
    return alertmanager_api.report(code)
