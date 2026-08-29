"""`probe.py alerts` -- DOWN history reconstructed from Loki, since Kuma keeps only current state.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.

TWO streams are read. monitor-bridge's container log covers the checks it runs; the host crons
push Kuma directly and so leave no trace there, which is why `{job="syslog"} status=down` is read
alongside it. Reading only the first left the whole backup/drift plane with no episode anywhere.
"""

import json
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core
from probe_core import _rows_from_loki, curl_argv, loki_endpoint, loki_query_url


# alert history (pure)
# monitor-bridge is the homelab's alert brain: every INTERVAL it pushes each check's
# state to a Kuma push monitor and logs "[<ts>] DOWN <name> - <msg> (<n> cycles)" for
# any check that's firing. Kuma keeps only current state; Loki keeps the log lines
# (31d retention), so the history of *what alerted, when* is these DOWN lines. This
# collapses the every-cycle repeats into one row per firing episode.
#
# It is NOT the only alert path, which is why this command reads two streams. Several host
# crons push their own Kuma monitors directly and never pass through monitor-bridge, so
# monitor-bridge's container log contains nothing about them — it polls no Kuma state. Reading
# only that stream made the backup plane's sole DOWN signal invisible: measured 2026-08-22,
# 465 `longhorn-backup-health: status=down` lines over 7 days appeared in no episode list,
# while `monitor_status{monitor_name="Manifest Prune Drift"}` read 0 and `alerts --check
# manifest` printed "no DOWN alerts". See SYSLOG_ALERT_LOGQL below for the second stream.
ALERT_LOGQL = '{container="monitor-bridge"} |= "DOWN"'
_CHICAGO = ZoneInfo("America/Chicago")
# "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed ... (2 cycles)"
_DOWN_RE = re.compile(r"^\[[^\]]+\] DOWN (?P<name>\S+) - (?P<msg>.*)$")
_CYCLES_SUFFIX_RE = re.compile(r"\s*\(\d+ cycles?\)\s*$")


def parse_down_line(line):
    """(check_name, msg) for a monitor-bridge DOWN log line, else None. The
    trailing "(N cycles)" consecutive-down counter is stripped from msg."""
    m = _DOWN_RE.match(line)
    if not m:
        return None
    return m["name"], _CYCLES_SUFFIX_RE.sub("", m["msg"])


# The second alert path: host crons that push Kuma directly and log through `logger`. rsyslog
# prefixes every one of those lines, so the shape is NOT the bare "<tag>: status=down <msg>" a
# reading of the cron scripts suggests — it is
#   "<iso-ts> <host> <tag>: status=down <msg>"
# and, when the push itself fails (the case where syslog is the ONLY record, since Kuma never
# learned),
#   "<iso-ts> <host> <tag>: push failed (status=down: <msg>)"
# Measured over 7 days on 2026-08-22, `|= "status=down"` matched exactly three tags —
# longhorn-backup-health (465 lines), manifest-prune-check (2) and claude-otel-health (1) — so
# the filter is precise, not a net that drags in unrelated syslog traffic.
#
# COVERAGE IS PARTIAL AND DELIBERATE. Two pushers emit no `status=` token at all and stay
# invisible here: secret-rotation-audit logs a bare reason string, and live_drift_check's cron
# pipes nothing to `logger`. Both fixes are one-line edits to files this change does not own
# (roles/k8s/.../secret-rotation-audit.sh.j2 and setup/k3s/tasks/health-crons.yml). Confirmed
# absent, not merely unmatched: a 7-day Loki query for either name returned "no logs".
#
# EVERY daniel-pi CRON IS INVISIBLE HERE, AND ADDING `logger` TO ONE DOES NOT FIX IT. The Pi
# ships no host logs at all: rsyslog is inactive there and /var/log/syslog does not exist, so
# the `{job="syslog"}` stream this reads carries nothing from that host, and the Pi's own
# promtail (roles/containers/promtail/templates/promtail.yml.j2:46) scrapes only the
# `pi-containers` job. `logger` on the Pi therefore reaches the local journal and stops. That
# is how a real "Daniel Pi Recovery" DOWN on 2026-08-29 — autoheal exited and stayed down for
# ~50 min — read as "no DOWN alerts in the last 7d" while the monitor was live-DOWN. The fix
# is to ship Pi host logs (enable rsyslog, or a promtail journal scrape), NOT to add a
# `logger` call to pi-recovery-health.sh.j2, which would land inert.
SYSLOG_ALERT_LOGQL = '{job="syslog"} |= "status=down"'
_SYSLOG_LINE_RE = re.compile(
    r"^\S+\s+\S+\s+(?P<name>[A-Za-z0-9_.-]+?)(?:\[\d+\])?:\s+(?P<rest>.*status=down.*)$"
)
# The closing paren is OPTIONAL because rsyslog truncates a long line — observed on
# longhorn-backup-health, whose status message runs past the limit and arrives with no closing
# paren at all. Anchoring on `\)$` dropped those lines to the raw fallback below, printing the
# "push failed (status=down: " scaffolding as if it were the message.
_SYSLOG_PUSH_FAILED_RE = re.compile(r"^push failed \(status=down:\s*(?P<msg>.*?)\)?$")
_SYSLOG_STATUS_RE = re.compile(r"^status=down\s*(?P<msg>.*)$")


def parse_syslog_down_line(line):
    """(cron_tag, msg) for a host cron's syslog DOWN line, else None.

    The tag is the episode name, matching what `--check` filters on — `manifest-prune-check`,
    `longhorn-backup-health`. Those are machine names like monitor-bridge's own check names,
    not Kuma display names, so one `--check` substring keeps working across both streams.

    A failed push keeps its "push failed:" prefix in the message. That is the operator's cue
    that Kuma never learned about this DOWN, so syslog is the only place it is recorded.
    """
    m = _SYSLOG_LINE_RE.match(line)
    if not m:
        return None
    rest = m["rest"]
    hit = _SYSLOG_STATUS_RE.match(rest)
    if hit:
        return m["name"], hit["msg"].strip()
    hit = _SYSLOG_PUSH_FAILED_RE.match(rest)
    if hit:
        return m["name"], f"push failed: {hit['msg'].strip()}"
    return m["name"], rest.strip()


# Each alert stream with the parser that reads its line shape. run_alerts queries both and
# merges the rows: LogQL cannot OR two stream selectors that share no label name, and
# `container` (monitor-bridge) and `job` (syslog) share none.
ALERT_SOURCES = (
    (ALERT_LOGQL, parse_down_line),
    (SYSLOG_ALERT_LOGQL, parse_syslog_down_line),
)


def alert_episodes(rows, gap_s=1800):
    """Collapse per-cycle DOWN samples into firing episodes.

    `rows` is an iterable of (epoch_ns, check_name, msg). Consecutive samples for the
    same check within `gap_s` seconds are one episode; a longer silence (the check
    recovered, then fired again) starts a new one. Returns episode dicts
    {name, first_ns, last_ns, cycles, msg} newest-episode-first (by last_ns). msg is
    the latest sample's — check messages evolve as the underlying value drifts."""
    by_name = defaultdict(list)
    for ns, name, msg in rows:
        by_name[name].append((int(ns), msg))
    episodes = []
    gap_ns = int(gap_s * 1e9)
    for name, samples in by_name.items():
        samples.sort()
        ep = None
        for ns, msg in samples:
            if ep is not None and ns - ep["last_ns"] <= gap_ns:
                ep["last_ns"] = ns
                ep["cycles"] += 1
                ep["msg"] = msg
            else:
                ep = {
                    "name": name,
                    "first_ns": ns,
                    "last_ns": ns,
                    "cycles": 1,
                    "msg": msg,
                }
                episodes.append(ep)
    episodes.sort(key=lambda e: e["last_ns"], reverse=True)
    return episodes


def _fmt_local(ns):
    return datetime.fromtimestamp(ns / 1e9, _CHICAGO).strftime("%Y-%m-%d %H:%M")


def _fmt_duration(ns):
    secs = ns / 1e9
    if secs < 60:
        return "1cyc"
    if secs < 3600:
        return f"{round(secs / 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def format_alert_episodes(episodes, days):
    """Human view: one aligned row per episode, newest first (America/Chicago, the
    container-log timezone). Empty -> a clear all-clear line."""
    if not episodes:
        return f"no DOWN alerts in the last {days:g}d"
    width = max(len(e["name"]) for e in episodes)
    header = (
        f"{len(episodes)} DOWN episode(s), last {days:g}d "
        "(monitor-bridge + host crons -> Kuma):"
    )
    lines = [header, ""]
    for e in episodes:
        dur = _fmt_duration(e["last_ns"] - e["first_ns"])
        lines.append(
            f"{_fmt_local(e['first_ns'])}  {dur:>6}  "
            f"{e['name']:<{width}}  {e['cycles']:>3}c  {e['msg'][:88]}"
        )
    return "\n".join(lines)


def alert_source_urls(base, days, limit):
    """The Loki URLs `alerts` fetches, one per stream in ALERT_SOURCES.

    `direction=forward` because episode reconstruction walks samples oldest-first; that is this
    command's need, not loki-query's — see run_query.
    """
    end_s = datetime.now(_CHICAGO).timestamp()
    start_s = end_s - days * 86400
    return [
        loki_query_url(
            base,
            logql,
            limit,
            start=int(start_s * 1e9),
            end=int(end_s * 1e9),
            direction="forward",
        )
        for logql, _ in ALERT_SOURCES
    ]


def run_alerts(ns):
    """Fetch DOWN log lines from every alert stream over the window and print firing episodes.

    Both streams are queried and their rows merged before episodes are built, so one episode
    list covers monitor-bridge's checks and the host crons that push Kuma directly. `--check`
    filters both, because both name episodes with a machine name rather than a Kuma display
    name.
    """
    base, pin = loki_endpoint()
    urls = alert_source_urls(base, ns.days, ns.limit)
    if ns.dry_run:
        for url in urls:
            print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    raw, rows, truncated = [], [], []
    for url, (logql, parser) in zip(urls, ALERT_SOURCES):
        fetched = _rows_from_loki(json.loads(core.fetch(url, resolve=pin)))
        # Per stream, not on the merged list: one stream hitting the cap says nothing about
        # the other, and reporting the union would cry truncation whenever the totals summed
        # past the limit.
        if len(fetched) >= ns.limit:
            truncated.append(logql)
        raw.extend(fetched)
        for ns_ts, line in fetched:
            parsed = parser(line)
            if parsed is None:
                continue
            name, msg = parsed
            if ns.check and ns.check.lower() not in name.lower():
                continue
            rows.append((ns_ts, name, msg))
    raw.sort()
    if ns.raw:
        print("\n".join(line for _, line in raw) or "no logs")
    else:
        episodes = alert_episodes(rows, ns.gap_min * 60)
        if ns.json:
            print(json.dumps(episodes, indent=2))
        else:
            print(format_alert_episodes(episodes, ns.days))
    for logql in truncated:
        print(
            f"\n(warning: hit --limit {ns.limit} log lines on {logql} — results may be "
            "truncated; raise --limit or narrow --days)"
        )
    return 0
