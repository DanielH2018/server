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
    avail = prom_scalar("node_memory_MemAvailable_bytes")
    total = prom_scalar("node_memory_MemTotal_bytes")
    oom = prom_scalar("sum(increase(container_oom_events_total[%s]))" % OOM_WINDOW)
    if avail is None or total is None or total == 0:
        return False, "memory metric unavailable"
    used_pct = 100.0 * (1 - avail / total)
    problems = []
    if used_pct > MEM_MAX_PCT:
        problems.append("mem %.0f%%" % used_pct)
    if oom and oom >= 1:
        problems.append("%.0f OOM in %s" % (oom, OOM_WINDOW))
    if problems:
        return False, "; ".join(problems)
    return True, "mem %.0f%%, no OOM" % used_pct


CHECKS = [
    ("backup", _env("KUMA_PUSH_KOPIA", ""), check_backup),
    ("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    ("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    ("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
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
