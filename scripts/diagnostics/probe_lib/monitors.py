"""`probe.py monitors` -- what is down, read from the alert plane.

Three reads, in order:

1. `ALERTS{alertstate="firing"}` from Prometheus, rolled up by `severity` and `service`. The
   Watchdog alert is the plane's own heartbeat: rule evaluation always fires it, so a result
   without it proves nothing and the read exits PROBE_UNREADABLE (failure class 3).
2. The active alerts Alertmanager holds that `ALERTS` lacks, folded into the same rollup. The
   loki-homelab ruler sends its alerts to Alertmanager directly, so `ALERTS` never carries
   them (`HostOutOfMemoryKill`, `LokiPiLogsSilent`).
3. Alertmanager's active silences (`alertmanager_api.report`).

`--checks [NAME]` reads each monitor-bridge check's verdict instead (`monitor_checks`): the
red checks that fire nothing, because a streak holds them, a gate skips them or no rule pages.
"""

from collections import Counter, defaultdict

import sys as _sys


# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import alertmanager_api, core, monitor_checks
from diagnostics.probe_lib.core import prom_endpoint, prom_query_url
from lib.exit_codes import PROBE_UNREADABLE

ALERTS_QUERY = 'ALERTS{alertstate="firing"}'
WATCHDOG = "Watchdog"
# The severities that make `monitors` exit 1. `info` alerts are listed as notes.
PAGING_SEVERITIES = ("critical", "warning")

MONITORS_HELP = (
    "firing alerts (ALERTS, Alertmanager) by severity and service, and active silences; "
    "--checks prints each monitor-bridge check's value, note and last-run age instead",
    "no paging alert fires (--checks: no check is down or stale)",
    "a critical or warning alert fires (--checks: a check is down or stale)",
)


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


def _collapse(entries):
    """Sorted lines for (service, alert, origin) entries, each reason printed once. Pure.

    An alert on one service reads `  service: alert`. An alert on several reads
    `  alert: service-a, service-b`, and a service repeated reads `service x4`.
    """
    by_alert = defaultdict(Counter)
    for service, alert, origin in entries:
        by_alert[alert, origin][service] += 1
    lines = []
    for (alert, origin), services in by_alert.items():
        parts = [f"{svc} x{n}" if n > 1 else svc for svc, n in sorted(services.items())]
        if len(services) == 1:
            (service,) = services
            repeat = f" x{services[service]}" if services[service] > 1 else ""
            lines.append(f"  {service}: {alert}{origin}{repeat}")
        else:
            lines.append(f"  {alert}{origin}: {', '.join(parts)}")
    return sorted(lines)


def format_alerts(data, held):
    """(lines, exit code) for the firing-alert rollup. Pure.

    `data` is the parsed response to `ALERTS_QUERY`, or a string saying why it went unread.
    `held` is Alertmanager's active-alert list, or a string saying why it went unread; its
    alerts that `ALERTS` lacks join the rollup marked `(Alertmanager only)`. A non-shadow `critical` or `warning` alert exits 1. Shadow alerts and `info` alerts are
    listed under their own headings and never change the code: a shadow rule is still under
    its soak. No Watchdog, or an unread response, exits
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
        with_severity = f"{name}{{severity={severity}}}"
        if labels.get("shadow") == "true":
            shadow.append((service, with_severity, origin))
        elif severity in PAGING_SEVERITIES:
            paging[severity].append((service, name, origin))
        else:
            notes.append((service, with_severity, origin))
    count = sum(len(v) for v in paging.values())
    lines = [f"{count} paging alert(s) firing"]
    for severity in PAGING_SEVERITIES:
        if paging[severity]:
            lines += [f"{severity}:", *_collapse(paging[severity])]
    if notes:
        lines += ["notes (not paging):", *_collapse(notes)]
    if shadow:
        lines += ["shadow rules firing (not counted):"]
        lines += _collapse(shadow)
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
    """Print the firing-alert rollup, then the active silences.

    Exit 0 = no paging alert fires, and every source was read.
    """
    if ns.checks is not None:
        return monitor_checks.print_checks(ns)
    base, pin = prom_endpoint()
    alerts_url = prom_query_url(base, ALERTS_QUERY)
    am_base, am_pin = alertmanager_api.endpoint()
    held_url = alertmanager_api.active_alerts_url(am_base)
    if ns.dry_run:
        core.print_dry_run(alerts_url, resolve=pin)
        core.print_dry_run(held_url, resolve=am_pin)
        return core.print_dry_run(
            alertmanager_api.silences_url(am_base), resolve=am_pin
        )
    lines, code = format_alerts(_read(alerts_url, pin), _read_held(held_url, am_pin))
    out = _sys.stderr if code == PROBE_UNREADABLE else _sys.stdout
    print("\n".join(lines), file=out)
    return alertmanager_api.report(code)


def add_parser(sub, verdict_help) -> None:
    """Add `monitors` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    mp = sub.add_parser("monitors", **verdict_help(*MONITORS_HELP))
    mp.add_argument(
        "--checks",
        nargs="?",
        const="",
        metavar="NAME",
        help="each monitor-bridge check's verdict instead of the alerts; NAME narrows to the "
        "checks whose name contains it",
    )
