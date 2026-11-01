"""`probe.py alerts` -- firing-alert history, collapsed to episodes, from Prometheus' `ALERTS`.

One `query_range` over `--days` returns a sample for every step an alert was firing. Each
series' consecutive samples are one episode; a gap longer than the splitting gap starts the
next. Prometheus keeps 30 days (`observability_prometheus_retention`), so a wider window
returns nothing older.

Shadow alerts are listed and marked: their batch still pages through Kuma, and this is where
their history is read. Watchdog fires always and is left out.

The loki-homelab ruler's alerts are missing: the ruler sends them to Alertmanager directly, so
they never reach `ALERTS`, and Alertmanager keeps no history. `probe.py monitors` shows the
ones firing now.
"""

import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlencode

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path; pyproject's `pythonpath` is a pytest setting. This has to sit
# ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core, pi_plane
from diagnostics.probe_lib.core import PI_HOST, prom_endpoint
from lib.exit_codes import PROBE_UNREADABLE

QUERY = 'ALERTS{alertstate="firing",alertname!="Watchdog"}'
# Prometheus refuses a range query over 11,000 points per series, so the step widens with
# the window: 60s up to ~6.9 days, then whatever keeps the count under this.
MAX_POINTS = 10_000
MIN_STEP_S = 60
# Two missed steps split an episode, so one skipped rule evaluation does not.
_GAP_STEPS = 2

# A Pi job is one whose static target names the Pi: the scrape jobs `pi_plane` derives, and
# `blackbox-pi-ports`, whose targets are the Pi's published container ports (PiPortDead).
_JOB_NAME_RE = re.compile(r"^\s*-\s*job_name:\s*(\S+)")
_PI_PORTS_MARKER = "hostvars['daniel-pi']"


def step_seconds(days):
    """The query step for a `days` window: MIN_STEP_S, widened to stay under MAX_POINTS."""
    return max(MIN_STEP_S, math.ceil(days * 86400 / MAX_POINTS))


def range_url(base, days, now):
    """The `query_range` URL for QUERY over the `days` before epoch `now`."""
    params = {
        "query": QUERY,
        "start": f"{now - days * 86400:.0f}",
        "end": f"{now:.0f}",
        "step": f"{step_seconds(days)}s",
    }
    return f"{base}/api/v1/query_range?" + urlencode(params)


def pi_alert_jobs(text):
    """The scrape jobs in prometheus.yaml.j2 `text` whose targets are on daniel-pi."""
    jobs, pending = set(pi_plane.declared_pi_job_names(text)), None
    for line in text.splitlines():
        m = _JOB_NAME_RE.match(line)
        if m:
            pending = m.group(1)
        elif _PI_PORTS_MARKER in line and "targets:" in line and pending:
            jobs.add(pending)
    return jobs


def keep_series(labels, check, pi_jobs):
    """Whether `--check` and `--pi` admit an alert series with `labels`. Pure.

    `check` is a substring of the alert name or its `service`; `pi_jobs` is None without
    `--pi`, else the Pi's scrape jobs. A Pi alert carries one of those jobs, or names the Pi
    as its `service` (PiHostDown, the Pi's heartbeat rules).
    """
    if check:
        needle = check.lower()
        names = (labels.get("alertname", ""), labels.get("service", ""))
        if not any(needle in n.lower() for n in names):
            return False
    if pi_jobs is not None:
        return labels.get("job") in pi_jobs or labels.get("service") == PI_HOST
    return True


def alert_episodes(result, step_s, gap_s=None):
    """Collapse a `query_range` matrix into firing episodes, newest-ending first. Pure.

    `result` is the response's `data.result`. Series that share an alert name, `service`,
    severity and shadow flag are merged first, so an alert firing per instance or per cgroup
    is one row rather than several identical ones. Samples within the splitting gap of each
    other are one episode: `gap_s` when the operator pins it, else `_GAP_STEPS` query steps.
    `samples` counts the distinct steps the alert fired at. Returns dicts {alertname, service,
    severity, shadow, first_s, last_s, samples}.
    """
    gap = gap_s if gap_s is not None else step_s * _GAP_STEPS
    merged = defaultdict(set)
    for series in result:
        labels = series.get("metric") or {}
        key = (
            labels.get("alertname", "?"),
            labels.get("service", "?"),
            labels.get("severity", "?"),
            labels.get("shadow") == "true",
        )
        merged[key].update(float(t) for t, _ in series.get("values") or [])
    episodes = []
    for (alertname, service, severity, shadow), times in merged.items():
        ep = None
        for t in sorted(times):
            if ep is not None and t - ep["last_s"] <= gap:
                ep["last_s"] = t
                ep["samples"] += 1
                continue
            ep = {
                "alertname": alertname,
                "service": service,
                "severity": severity,
                "shadow": shadow,
                "first_s": t,
                "last_s": t,
                "samples": 1,
            }
            episodes.append(ep)
    episodes.sort(key=lambda e: (e["last_s"], e["first_s"]), reverse=True)
    return episodes


# UTC, and said to be UTC in the header: daniel-box runs UTC, so every incident timestamp an
# operator compares against is UTC.
def _fmt_utc(s):
    return datetime.fromtimestamp(s, UTC).strftime("%Y-%m-%d %H:%M")


def _fmt_utc_end(first_s, last_s):
    """The episode's end, dropping the date when it matches the start's."""
    start, end = _fmt_utc(first_s), _fmt_utc(last_s)
    return end[11:] if end[:10] == start[:10] else end


def _fmt_duration(secs, step_s):
    if secs < step_s:
        return "1 step"
    if secs < 3600:
        return f"{round(secs / 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def format_alert_episodes(episodes, days, step_s):
    """One aligned row per episode, newest first, with both ends of each. Pure.

    An empty list renders an all-clear line naming the window.
    """
    if not episodes:
        return f"no firing alerts in the last {days:g}d"
    names = [f"{e['service']}/{e['alertname']}" for e in episodes]
    width = max(len(n) for n in names)
    lines = [
        f"{len(episodes)} firing episode(s), last {days:g}d, times UTC, "
        f"{step_s}s resolution (Prometheus ALERTS):",
        "",
    ]
    for e, name in zip(episodes, names, strict=True):
        span = f"{_fmt_utc(e['first_s'])} -> {_fmt_utc_end(e['first_s'], e['last_s'])}"
        dur = _fmt_duration(e["last_s"] - e["first_s"], step_s)
        tag = " shadow" if e["shadow"] else ""
        lines.append(f"{span:<36} {dur:>6}  {name:<{width}}  {e['severity']}{tag}")
    return "\n".join(lines)


def run_alerts(ns, now=None):
    """Read `ALERTS` over `--days` and print its firing episodes.

    Exit 0 whenever the history was read, whatever it holds; PROBE_UNREADABLE when it was not.
    """
    base, pin = prom_endpoint()
    now = now if now is not None else datetime.now(UTC).timestamp()
    url = range_url(base, ns.days, now)
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    try:
        data = core.fetch_parsed(url, resolve=pin)
    except core.Unreadable:
        return PROBE_UNREADABLE  # Unreadable printed its own message
    if data.get("status") != "success":
        detail = data.get("error") or f"status {data.get('status')!r}"
        print(f"ALERTS history unread: {detail}", file=_sys.stderr)
        return PROBE_UNREADABLE
    pi_jobs = (
        pi_alert_jobs(pi_plane.PROMETHEUS_TEMPLATE_PATH.read_text()) if ns.pi else None
    )
    result = [
        s
        for s in data.get("data", {}).get("result", [])
        if keep_series(s.get("metric") or {}, ns.check, pi_jobs)
    ]
    step_s = step_seconds(ns.days)
    gap_s = None if ns.gap_min is None else ns.gap_min * 60
    episodes = alert_episodes(result, step_s, gap_s)
    if ns.json:
        print(json.dumps(episodes, indent=2))
    else:
        print(format_alert_episodes(episodes, ns.days, step_s))
    return 0
