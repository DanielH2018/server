#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.12-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def _env(name, default):
    return os.environ.get(name, default)


INTERVAL = int(_env("INTERVAL", "300"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
PROM_URL = _env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
KOPIA_URL = _env("KOPIA_URL", "http://kopia:51515").rstrip("/")
KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")

BACKUP_PATH = _env("BACKUP_SOURCE_PATH", "/data/home/ubuntu/server/containers")
BACKUP_MAX_AGE_H = float(_env("BACKUP_MAX_AGE_H", "30"))
DISK_MOUNTPOINTS = [m.strip() for m in _env("DISK_MOUNTPOINTS", "/").split(",") if m.strip()]
DISK_MAX_PCT = float(_env("DISK_MAX_PCT", "90"))
CERT_MIN_DAYS = float(_env("CERT_MIN_DAYS", "14"))
MEM_MAX_PCT = float(_env("MEM_MAX_PCT", "90"))
OOM_WINDOW = _env("OOM_WINDOW", "1h")
CPU_WINDOW = _env("CPU_WINDOW", "15m")
CPU_THROTTLE_PCT = float(_env("CPU_THROTTLE_PCT", "25"))
CPU_MIN_THROTTLED_CORES = float(_env("CPU_MIN_THROTTLED_CORES", "0.05"))
RESTART_WINDOW = _env("RESTART_WINDOW", "15m")
RESTART_MAX = float(_env("RESTART_MAX", "3"))
TRAEFIK_5XX_PCT = float(_env("TRAEFIK_5XX_PCT", "5"))
TRAEFIK_MIN_RPS = float(_env("TRAEFIK_MIN_RPS", "0.05"))


# --- HTTP / parsing helpers (pure-ish, unit-tested) -------------------------

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-bridge"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (internal URLs)
        return json.load(resp)


def prom_scalar(promql):
    """Run an instant query; return the first result's value as float, or None if empty."""
    url = PROM_URL + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError("prometheus query status=%s" % data.get("status"))
    result = data.get("data", {}).get("result", [])
    if not result:
        return None
    return float(result[0]["value"][1])


def prom_vector(promql):
    """Run an instant query; return [(labels: dict, value: float), ...] (empty if none).

    Unlike prom_scalar this keeps each series' labels, so checks can name *which*
    container / target / route is failing.
    """
    url = PROM_URL + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError("prometheus query status=%s" % data.get("status"))
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in data.get("data", {}).get("result", [])
    ]


def parse_rfc3339(ts):
    """Parse an RFC3339 timestamp, tolerating nanosecond precision and a trailing 'Z'.

    datetime.fromisoformat only accepts 3- or 6-digit fractional seconds, but Kopia
    emits 9 (nanoseconds), so truncate the fractional part to microseconds first.
    """
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    if "." in ts:
        head, frac = ts.split(".", 1)
        digits = ""
        rest = ""
        for i, ch in enumerate(frac):
            if ch.isdigit():
                digits += ch
            else:
                rest = frac[i:]
                break
        ts = head + "." + digits[:6] + rest
    return datetime.fromisoformat(ts)


def parse_duration(s):
    """Parse a Prometheus-style duration ('900s', '15m', '1h', '2d') to seconds (float).

    A bare number is treated as seconds. The n8n check evaluates its failure window in
    Python (unlike the *_WINDOW vars that are interpolated straight into PromQL, which
    Prometheus parses), so it needs this.
    """
    s = str(s).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


def backup_age_hours(sources_json, path, now=None):
    """Return (age_hours, error_count) for the Kopia source matching `path`.

    Raises LookupError if the source or its lastSnapshot is missing.
    """
    now = now or datetime.now(timezone.utc)
    srcs = [s for s in sources_json.get("sources", []) if s.get("source", {}).get("path") == path]
    if not srcs:
        raise LookupError("no Kopia source for %s" % path)
    last = srcs[0].get("lastSnapshot")
    if not last:
        raise LookupError("no snapshot recorded yet")
    end = last.get("endTime") or last.get("startTime")
    age_h = (now - parse_rfc3339(end)).total_seconds() / 3600.0
    errs = int(last.get("stats", {}).get("errorCount", 0))
    return age_h, errs


# --- checks: each returns (ok, msg) -----------------------------------------

def check_backup():
    data = _get_json(KOPIA_URL + "/api/v1/sources")
    try:
        age_h, errs = backup_age_hours(data, BACKUP_PATH)
    except LookupError as e:
        return False, str(e)
    if errs:
        return False, "last snapshot had %d errors (%.1fh ago)" % (errs, age_h)
    if age_h > BACKUP_MAX_AGE_H:
        return False, "last snapshot %.1fh ago (> %.0fh)" % (age_h, BACKUP_MAX_AGE_H)
    return True, "last snapshot %.1fh ago, 0 errors" % age_h


def check_disk():
    breaching = []
    for mp in DISK_MOUNTPOINTS:
        sel = '{mountpoint="%s"}' % mp
        avail = prom_scalar("node_filesystem_avail_bytes" + sel)
        size = prom_scalar("node_filesystem_size_bytes" + sel)
        if avail is None or size is None or size == 0:
            return False, "metric unavailable for %s" % mp
        used_pct = 100.0 * (1 - avail / size)
        if used_pct > DISK_MAX_PCT:
            breaching.append("%s %.0f%%" % (mp, used_pct))
    if breaching:
        return False, "disk over %.0f%%: %s" % (DISK_MAX_PCT, ", ".join(breaching))
    return True, "all mounts under %.0f%%" % DISK_MAX_PCT


def check_cert():
    days = prom_scalar("(min(traefik_tls_certs_not_after) - time()) / 86400")
    if days is None:
        return False, "cert metric unavailable"
    if days < CERT_MIN_DAYS:
        return False, "cert expires in %.1fd (< %.0fd)" % (days, CERT_MIN_DAYS)
    return True, "cert valid %.0fd" % days


def check_mem():
    # Host memory pressure only. Per-container OOM kills are reported (with the
    # offending container named) by check_oom — single source of truth.
    avail = prom_scalar("node_memory_MemAvailable_bytes")
    total = prom_scalar("node_memory_MemTotal_bytes")
    if avail is None or total is None or total == 0:
        return False, "memory metric unavailable"
    used_pct = 100.0 * (1 - avail / total)
    if used_pct > MEM_MAX_PCT:
        return False, "mem %.0f%% (> %.0f%%)" % (used_pct, MEM_MAX_PCT)
    return True, "mem %.0f%%" % used_pct


def _top_offenders(vector, label, predicate):
    """Names (by `label`) of series matching predicate(value), sorted by value desc."""
    hits = [(m.get(label, "?"), v) for m, v in vector if predicate(v)]
    hits.sort(key=lambda nv: -nv[1])
    return hits


def check_restarts():
    """Containers restarting more than RESTART_MAX times within RESTART_WINDOW.

    Catches crash-loops that an intermittent up-check can miss.
    """
    vec = prom_vector('changes(container_start_time_seconds{name!=""}[%s])' % RESTART_WINDOW)
    offenders = _top_offenders(vec, "name", lambda v: v > RESTART_MAX)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) restarting >%.0fx in %s: %s" % (
            len(offenders), RESTART_MAX, RESTART_WINDOW, desc)
    return True, "no restart loops in %s" % RESTART_WINDOW


def check_oom():
    """Containers OOM-killed within OOM_WINDOW, naming each one.

    Closes the loop on the per-container memory limits (deploy.resources). If cAdvisor
    doesn't expose container_oom_events_total the query is empty and this stays green.
    """
    vec = prom_vector(
        'sum(increase(container_oom_events_total{name!=""}[%s])) by (name)' % OOM_WINDOW)
    offenders = _top_offenders(vec, "name", lambda v: v > 0)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) OOM-killed in %s: %s" % (len(offenders), OOM_WINDOW, desc)
    return True, "no OOM kills in %s" % OOM_WINDOW


def check_cpu_throttle():
    """Containers under *sustained* CPU CFS throttling within CPU_WINDOW, naming each one.

    A container pinned at its `deploy.resources` cpu limit is throttled (slowed) without
    OOMing, restarting, or 5xx-ing — invisible to the other checks. We alert only when
    BOTH conditions hold, so noise doesn't page:

      1. throttled/total CFS *periods* > CPU_THROTTLE_PCT — the fraction of enforcement
         periods that hit the cap (the cue the resources() macro names for raising a cap);
      2. throttled *seconds* per second > CPU_MIN_THROTTLED_CORES — the absolute CPU time
         (in cores) actually lost to throttling.

    Condition 1 alone fires constantly for tiny low-limit utility containers that briefly
    burst over their per-period slice while losing negligible absolute time (e.g. a 0.1-cpu
    sidecar at 90% throttled periods but 0.0001 cores lost) — a perpetual false `down`. The
    cores floor — the same volume-floor idea as check_traefik_5xx's TRAEFIK_MIN_RPS — gates
    those out, so the monitor pushes `up` and only goes `down` on genuine starvation.
    Containers with no cpu limit give 0/0 -> NaN for condition 1 (NaN comparisons are False)
    and are ignored; if cAdvisor doesn't expose the cfs metrics both queries are empty -> green.
    """
    ratio_vec = prom_vector(
        'sum(rate(container_cpu_cfs_throttled_periods_total{name!=""}[%s])) by (name) '
        '/ sum(rate(container_cpu_cfs_periods_total{name!=""}[%s])) by (name)'
        % (CPU_WINDOW, CPU_WINDOW))
    lost_cores = dict(
        (m.get("name", "?"), v) for m, v in prom_vector(
            'sum(rate(container_cpu_cfs_throttled_seconds_total{name!=""}[%s])) by (name)'
            % CPU_WINDOW))
    threshold = CPU_THROTTLE_PCT / 100.0
    offenders = []
    for m, ratio in ratio_vec:
        name = m.get("name", "?")
        lost = lost_cores.get(name, 0.0)
        if ratio > threshold and lost > CPU_MIN_THROTTLED_CORES:
            offenders.append((name, ratio, lost))
    offenders.sort(key=lambda nrl: -nrl[1])
    if offenders:
        desc = ", ".join(
            "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5])
        return False, "%d container(s) CPU-throttled >%.0f%% & >%.2f cores in %s: %s" % (
            len(offenders), CPU_THROTTLE_PCT, CPU_MIN_THROTTLED_CORES, CPU_WINDOW, desc)
    return True, "no sustained CPU throttling in %s" % CPU_WINDOW


def check_targets_down():
    """Any Prometheus scrape target reporting up==0 (monitoring going blind)."""
    vec = prom_vector("up")
    down = sorted({m.get("job") or m.get("instance") or "?" for m, v in vec if v == 0})
    if down:
        return False, "%d target(s) down: %s" % (len(down), ", ".join(down))
    return True, "all %d targets up" % len(vec)


def check_traefik_5xx():
    """Elevated 5xx ratio at the proxy, guarded by a request-rate floor.

    The TRAEFIK_MIN_RPS floor stops a single error on near-zero traffic from tripping
    a 100%-error-ratio false alarm.
    """
    total = prom_scalar("sum(rate(traefik_service_requests_total[5m]))")
    errors = prom_scalar('sum(rate(traefik_service_requests_total{code=~"5.."}[5m]))') or 0.0
    if total is None or total < TRAEFIK_MIN_RPS:
        return True, "traffic below floor (%.3f rps)" % (total or 0.0)
    pct = 100.0 * errors / total
    if pct > TRAEFIK_5XX_PCT:
        return False, "5xx %.1f%% of %.2f rps (> %.0f%%)" % (pct, total, TRAEFIK_5XX_PCT)
    return True, "5xx %.1f%% of %.2f rps" % (pct, total)


CHECKS = [
    ("backup", _env("KUMA_PUSH_KOPIA", ""), check_backup),
    ("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    ("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    ("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
    ("restarts", _env("KUMA_PUSH_RESTARTS", ""), check_restarts),
    ("oom", _env("KUMA_PUSH_OOM", ""), check_oom),
    ("cpu", _env("KUMA_PUSH_CPU", ""), check_cpu_throttle),
    ("targets", _env("KUMA_PUSH_TARGETS", ""), check_targets_down),
    ("traefik5xx", _env("KUMA_PUSH_TRAEFIK", ""), check_traefik_5xx),
]


def log(*args):
    print("[%s]" % datetime.now().isoformat(timespec="seconds"), *args, flush=True)


def push(token, ok, msg):
    if not token:
        log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _get_json("%s/api/push/%s?%s" % (KUMA_URL, token, qs))
    except Exception as e:  # best-effort heartbeat; never crash the loop
        log("push failed (%s):" % msg, e)


def run_once():
    for name, token, fn in CHECKS:
        try:
            ok, msg = fn()
        except Exception as e:  # an unreachable source/metric must not kill the loop
            ok, msg = False, "%s check error: %s" % (name, e)
        log("OK  " if ok else "DOWN", name, "-", msg)
        push(token, ok, msg)


def main():
    once = "--once" in sys.argv
    log("monitor-bridge starting (interval=%ss, once=%s)" % (INTERVAL, once))
    while True:
        run_once()
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
