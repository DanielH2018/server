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
from datetime import datetime, timedelta, timezone


def _env(name, default):
    return os.environ.get(name, default)


INTERVAL = int(_env("INTERVAL", "300"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
# Touched after every completed cycle; the container healthcheck compares its mtime
# against ~3×INTERVAL. PID death already restarts the container, but a HANG only shows
# up as push silence in Kuma — the healthcheck lets autoheal restart on that too.
HEARTBEAT_FILE = _env("HEARTBEAT_FILE", "/tmp/heartbeat")
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
CPU_CONSECUTIVE = int(_env("CPU_CONSECUTIVE", "3"))
RESTART_WINDOW = _env("RESTART_WINDOW", "15m")
RESTART_MAX = float(_env("RESTART_MAX", "3"))
TRAEFIK_5XX_PCT = float(_env("TRAEFIK_5XX_PCT", "5"))
TRAEFIK_MIN_RPS = float(_env("TRAEFIK_MIN_RPS", "0.05"))
N8N_URL = _env("N8N_URL", "http://n8n:5678").rstrip("/")
N8N_API_KEY = _env("N8N_API_KEY", "")
N8N_FAIL_WINDOW = _env("N8N_FAIL_WINDOW", "15m")
N8N_FAIL_MAX = float(_env("N8N_FAIL_MAX", "0"))
GITOPS_STATE_DIR = _env("GITOPS_STATE_DIR", "/gitops-state")
GITOPS_MAX_AGE_S = float(_env("GITOPS_MAX_AGE_MIN", "90")) * 60
RENOVATE_STATE_DIR = _env("RENOVATE_STATE_DIR", "/renovate-state")
RENOVATE_MAX_AGE_S = float(_env("RENOVATE_MAX_AGE_MIN", "2160")) * 60

# Monthly kopia restore drill: the host cron (kopia-restore-drill.sh, kopia role)
# writes {"ts": epoch, "ok": bool, "msg": str} after each run; we alert on failure,
# staleness (cron broken / never ran), or a missing/corrupt state file.
RESTORE_DRILL_STATE = _env("RESTORE_DRILL_STATE", "/restore-drill/state.json")
RESTORE_DRILL_MAX_AGE_S = float(_env("RESTORE_DRILL_MAX_AGE_D", "35")) * 86400

# B2 storage usage: the daily host cron (kopia-b2-usage.sh, kopia role) writes
# {"ts": epoch, "ok": bool, "bytes": int, "msg": str} with the bucket's BILLABLE
# bytes (incl. hidden versions — what counts against the free tier). We alert when
# usage crosses the threshold, on probe failure, staleness, or missing state.
# 2.5d staleness = two missed daily runs + slack.
B2_USAGE_STATE = _env("B2_USAGE_STATE", "/b2-usage/state.json")
B2_USAGE_MAX_AGE_S = float(_env("B2_USAGE_MAX_AGE_D", "2.5")) * 86400
# Decimal GB (1e9), not GiB: B2 bills and displays decimal units, and the cap we're
# protecting is B2's "10 GB" free tier — GiB math would overstate the allowance ~7%.
B2_CAP_BYTES = float(_env("B2_CAP_GB", "10")) * 1e9
B2_USAGE_MAX_PCT = float(_env("B2_USAGE_MAX_PCT", "85"))

# Scrutiny SMART freshness: the collector cron runs daily (00:00) and has no usable
# container healthcheck (cron is PID 1) — a silently-dead collector only shows as
# aging collector_date values in the web API. 26h allows one run + slack.
SCRUTINY_URL = _env("SCRUTINY_URL", "http://scrutiny:8080").rstrip("/")
SCRUTINY_MAX_AGE_H = float(_env("SCRUTINY_MAX_AGE_H", "26"))

# Pi pressure: the 512MB Zero 2 W dies by swap-thrash, not by clean failures —
# 2026-06-11 (fwupd): hourly load5/core >1.7 episodes with healthcheck-timeout storms
# that no other monitor saw (containers stayed "restarting", never down long enough).
# Polled from the glances API already running on the Pi (zero added Pi footprint);
# the separate static Kuma HTTP monitor covers glances itself being down.
PI_GLANCES_URL = _env("PI_GLANCES_URL", "").rstrip("/")
PI_LOAD_MAX = float(_env("PI_LOAD_MAX", "1.5"))  # load5 per core
PI_MEM_MIN_MB = float(_env("PI_MEM_MIN_MB", "50"))
PI_DISK_MAX_PCT = float(_env("PI_DISK_MAX_PCT", "90"))

# HA automation-engine heartbeat: an HA time_pattern automation stamps
# input_datetime.ha_heartbeat with now() every minute, so its last_changed is fresh ONLY
# while HA's automation scheduler is executing. We poll HA's /api/states over the apps
# network (Bearer token) and go down when it's stale — catching a wedged-but-running HA
# (HTTP :8123 up, scheduler stuck) that the container healthcheck can't see. Empty
# URL/token = disabled (stays up), like N8N_API_KEY/PI_GLANCES_URL. 300s = 5 missed
# 1-min beats; rides out an HA restart/deploy. Seconds (no unit suffix) — kept a plain
# float here because parse_duration is defined below this config block.
HA_URL = _env("HA_URL", "").rstrip("/")
HA_TOKEN = _env("HA_TOKEN", "")
HA_HEARTBEAT_MAX_AGE_S = float(_env("HA_HEARTBEAT_MAX_AGE", "300"))
HA_HEARTBEAT_ENTITY = "input_datetime.ha_heartbeat"
# Consecutive-cycle hysteresis (like CPU_CONSECUTIVE) so a planned HA redeploy — which takes
# the API unreachable for ~120s and then leaves the scheduler a beat behind — doesn't page.
# 2 straight down cycles (~one full INTERVAL of continuous badness) before `down`.
HA_CONSECUTIVE = int(_env("HA_CONSECUTIVE", "2"))


# --- HTTP / parsing helpers (pure-ish, unit-tested) -------------------------

def _get_json(url, headers=None):
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers is not None:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
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


_cpu_breach_streak = 0


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

    On top of the two gates, CPU_CONSECUTIVE adds hysteresis: only the Nth consecutive
    breaching cycle goes `down` (~(N×INTERVAL)s of continuous throttling at the loop
    cadence). One- or two-cycle bursts — flaresolverr solving a challenge, homepage
    briefly hugging the cores floor — push `up` with the offender named in the msg, so
    the evidence stays in the bridge log without paging. A clean cycle resets the streak.
    """
    global _cpu_breach_streak
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
    if not offenders:
        _cpu_breach_streak = 0
        return True, "no sustained CPU throttling in %s" % CPU_WINDOW
    _cpu_breach_streak += 1
    desc = ", ".join(
        "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5])
    if _cpu_breach_streak < CPU_CONSECUTIVE:
        return True, "throttling streak %d/%d (not alerting yet): %s" % (
            _cpu_breach_streak, CPU_CONSECUTIVE, desc)
    return False, "%d container(s) CPU-throttled >%.0f%% & >%.2f cores for %d cycles: %s" % (
        len(offenders), CPU_THROTTLE_PCT, CPU_MIN_THROTTLED_CORES, _cpu_breach_streak, desc)


def check_targets_down():
    """Any Prometheus scrape target reporting up==0 (monitoring going blind)."""
    vec = prom_vector("up")
    down = sorted({m.get("job") or m.get("instance") or "?" for m, v in vec if v == 0})
    if down:
        return False, "%d target(s) down: %s" % (len(down), ", ".join(down))
    return True, "all %d targets up" % len(vec)


def check_traefik_5xx():
    """Elevated 5xx ratio per Traefik service, naming each offender.

    Per-service (not aggregate) for two reasons: the alert points at *which* backend is
    erroring, and a broken low-traffic service can't hide diluted below the threshold by
    healthy high-traffic ones. The TRAEFIK_MIN_RPS floor is per-service too — same idea
    as before, a single error on a near-idle route is not a 100%-error-ratio alarm.
    """
    total_vec = prom_vector(
        "sum(rate(traefik_service_requests_total[5m])) by (service)")
    err_rps = dict(
        (m.get("service", "?"), v) for m, v in prom_vector(
            'sum(rate(traefik_service_requests_total{code=~"5.."}[5m])) by (service)'))
    offenders = []
    total_rps = 0.0
    eligible = 0
    for m, rps in total_vec:
        total_rps += rps
        if rps < TRAEFIK_MIN_RPS:
            continue
        eligible += 1
        svc = m.get("service", "?")
        pct = 100.0 * err_rps.get(svc, 0.0) / rps
        if pct > TRAEFIK_5XX_PCT:
            offenders.append((svc, pct, rps))
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return False, "%d service(s) over %.0f%% 5xx: %s" % (
            len(offenders), TRAEFIK_5XX_PCT, desc)
    return True, "5xx ok: %d service(s) above floor, %.2f rps total" % (eligible, total_rps)


def n8n_failures(workflows_json, executions_json, window_s, now=None):
    """Failed executions of *active* workflows within the last `window_s` seconds.

    Returns [(workflow_name, count), ...] sorted by count desc. An execution counts only
    if its workflowId belongs to an active ("Prod") workflow AND its stoppedAt (fallback
    startedAt) is within the window. Pure — fed the n8n /workflows and /executions
    payloads, so it's unit-tested without HTTP (like backup_age_hours).
    """
    now = now or datetime.now(timezone.utc)
    active = {
        w["id"]: w.get("name") or w["id"]
        for w in workflows_json.get("data", [])
        if w.get("active")
    }
    cutoff = now - timedelta(seconds=window_s)
    counts = {}
    for ex in executions_json.get("data", []):
        wid = ex.get("workflowId")
        if wid not in active:
            continue
        ts = ex.get("stoppedAt") or ex.get("startedAt")
        if not ts:
            continue
        dt = parse_rfc3339(ts)
        if dt.tzinfo is None:  # n8n normally emits UTC 'Z'; assume UTC if a naive ts slips through
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue
        counts[wid] = counts.get(wid, 0) + 1
    pairs = [(active[wid], c) for wid, c in counts.items()]
    pairs.sort(key=lambda nc: -nc[1])
    return pairs


def gitops_alive(age_s, max_age_s):
    """Pure: is the deployer's last completed tick recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "deployer ran %.0fm ago" % (age_s / 60)
    return False, "deployer last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


def gitops_status(hold_sha):
    """Pure: is a rolled-back commit being held? Returns (ok, msg)."""
    if not hold_sha:
        return True, "no held deploy"
    return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]


def check_n8n():
    """Failed executions of active ("Prod") n8n workflows within N8N_FAIL_WINDOW.

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia).
    Empty N8N_API_KEY -> disabled (stays up) so it never false-pages before the operator
    sets the key. An unreachable/erroring API raises -> the loop renders it down with the
    error, like check_targets_down (a dead API surfaces, not silent-green).
    """
    if not N8N_API_KEY:
        return True, "n8n monitoring disabled (no API key)"
    headers = {"X-N8N-API-KEY": N8N_API_KEY}
    workflows = _get_json(N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers)
    executions = _get_json(N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers)
    offenders = n8n_failures(workflows, executions, parse_duration(N8N_FAIL_WINDOW))
    total = sum(c for _, c in offenders)
    if total > N8N_FAIL_MAX:
        desc = ", ".join("%s (%d)" % (n, c) for n, c in offenders[:5])
        return False, "%d active workflow(s) failed in %s: %s" % (
            len(offenders), N8N_FAIL_WINDOW, desc)
    return True, "no active-workflow failures in %s" % N8N_FAIL_WINDOW


def check_gitops_alive():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, GITOPS_MAX_AGE_S)


def check_gitops_status():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "hold_sha")) as fh:
            hold = fh.read().strip() or None
    except FileNotFoundError:
        hold = None
    return gitops_status(hold)


def renovate_alive(age_s, max_age_s):
    """Pure: is the notifier's last completed run recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "notifier ran %.0fm ago" % (age_s / 60)
    return False, "notifier last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


def check_renovate_alive():
    try:
        with open(os.path.join(RENOVATE_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (notifier never completed a run?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return renovate_alive(time.time() - ts, RENOVATE_MAX_AGE_S)


def scrutiny_freshness(summary, max_age_h, now=None):
    """`summary` is the data.summary dict of scrutiny's /api/summary."""
    now = now or datetime.now(timezone.utc)
    stale, n = [], 0
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        n += 1
        name = dev.get("device_name") or wwn
        cdate = (entry.get("smart") or {}).get("collector_date")
        if not cdate:
            stale.append("%s (no SMART data)" % name)
            continue
        age_h = (now - parse_rfc3339(cdate)).total_seconds() / 3600
        if age_h > max_age_h:
            stale.append("%s (last report %.1fh ago)" % (name, age_h))
    if not n:
        return False, "scrutiny reports no devices (collector never ran?)"
    if stale:
        return False, "stale SMART data: " + ", ".join(stale)
    return True, "%d device(s) reported within %gh" % (n, max_age_h)


def check_scrutiny():
    data = _get_json(SCRUTINY_URL + "/api/summary")
    return scrutiny_freshness((data.get("data") or {}).get("summary"), SCRUTINY_MAX_AGE_H)


def pi_pressure(load_json, mem_json, fs_json, load_max, mem_min_mb, disk_max_pct):
    """Pure: load per core, available-memory floor, or a full filesystem on the Pi.

    Fed glances /api/4/load, /api/4/mem and /api/4/fs payloads. load5 (not load1)
    matches the 5-min poll interval and rides out single-probe spikes; `available`
    (not `free`) is what the kernel can actually reclaim — the box thrashes when THAT
    runs out. The fs list is glances' *container* view: every entry is a bind-mount
    path, but they're all backed by the SD card device with the HOST usage percent —
    so filesystems are deduped by device_name (a filling SD card is the classic slow
    Pi death the server-only Root Disk check can't see). Missing fields and an empty
    fs list alert rather than silently passing (a glances plugin regression must
    surface, same principle as the other checks' unreachable-source handling).
    """
    cores = load_json.get("cpucore") or 0
    load5 = load_json.get("min5")
    avail = mem_json.get("available")
    devices = {}
    for fs in fs_json or []:
        dev, pct = fs.get("device_name"), fs.get("percent")
        if dev and pct is not None:
            devices[dev] = max(pct, devices.get(dev, 0.0))
    if not cores or load5 is None or avail is None or not devices:
        return False, "glances payload missing load/mem/fs fields"
    per_core = load5 / cores
    avail_mb = avail / 1048576.0
    problems = []
    if per_core > load_max:
        problems.append("load5 %.2f/core (> %.2f)" % (per_core, load_max))
    if avail_mb < mem_min_mb:
        problems.append("mem available %.0fMB (< %.0fMB)" % (avail_mb, mem_min_mb))
    for dev, pct in sorted(devices.items(), key=lambda dp: -dp[1]):
        if pct > disk_max_pct:
            problems.append("disk %s %.0f%% (> %.0f%%)" % (dev, pct, disk_max_pct))
    if problems:
        return False, "; ".join(problems)
    return True, "load5 %.2f/core, %.0fMB available, disk %.0f%%" % (
        per_core, avail_mb, max(devices.values()))


def check_pi_pressure():
    """Swap-thrash / overload early warning for the memory-constrained Pi.

    Empty PI_GLANCES_URL -> disabled (stays up), like check_n8n without an API key.
    An unreachable glances raises -> the loop renders it down with the error.
    """
    if not PI_GLANCES_URL:
        return True, "pi monitoring disabled (no glances URL)"
    load = _get_json(PI_GLANCES_URL + "/api/4/load")
    mem = _get_json(PI_GLANCES_URL + "/api/4/mem")
    fs = _get_json(PI_GLANCES_URL + "/api/4/fs")
    return pi_pressure(load, mem, fs, PI_LOAD_MAX, PI_MEM_MIN_MB, PI_DISK_MAX_PCT)


def restore_drill(state, age_s, max_age_s):
    if not state.get("ok"):
        return False, "last restore drill FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last successful restore drill %.1fd ago (max %dd)" % (
            age_s / 86400, max_age_s / 86400)
    return True, "restore drill ok %.1fd ago: %s" % (age_s / 86400, state.get("msg", ""))


def check_restore_drill():
    try:
        with open(RESTORE_DRILL_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no restore-drill state (drill never ran?)"
    except (ValueError, TypeError):
        return False, "restore-drill state unparseable"
    return restore_drill(state, age_s, RESTORE_DRILL_MAX_AGE_S)


def b2_usage(state, age_s, max_age_s, cap_bytes, max_pct):
    """Pure: billable B2 bytes vs the plan cap, plus probe-failure/staleness.

    The threshold fires BEFORE the cap (default 85% of 10GB) — once the bucket is
    full, B2 rejects uploads and kopia's nightly snapshot starts failing, so the
    point is runway to prune/upgrade, not a post-mortem.
    """
    if not state.get("ok"):
        return False, "B2 usage probe FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "B2 usage data %.1fd old (max %.1fd)" % (
            age_s / 86400, max_age_s / 86400)
    try:
        used = float(state["bytes"])
    except (KeyError, TypeError, ValueError):
        return False, "B2 usage state missing/invalid bytes"
    pct = used / cap_bytes * 100
    msg = "B2 %.2f/%.0fGB billable (%.0f%% of plan)" % (
        used / 1e9, cap_bytes / 1e9, pct)
    if pct > max_pct:
        return False, msg + " — over %g%% threshold" % max_pct
    return True, msg


def check_b2_usage():
    try:
        with open(B2_USAGE_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no B2-usage state (probe never ran?)"
    except (ValueError, TypeError):
        return False, "B2-usage state unparseable"
    return b2_usage(state, age_s, B2_USAGE_MAX_AGE_S, B2_CAP_BYTES, B2_USAGE_MAX_PCT)


def ha_heartbeat_fresh(state, max_age_s, now=None):
    """`state` is HA's /api/states/input_datetime.ha_heartbeat payload.

    Its last_changed advances every minute only while HA's automation scheduler runs the
    heartbeat automation, so a stale (or missing) last_changed means HA is wedged or the
    automation never resumed after a restart — invisible to the HTTP healthcheck.
    """
    now = now or datetime.now(timezone.utc)
    lc = (state or {}).get("last_changed")
    if not lc:
        return False, "no heartbeat state (entity missing or never set)"
    age = (now - parse_rfc3339(lc)).total_seconds()
    if age > max_age_s:
        return False, "stale — automations last ran %.0fs ago (> %gs)" % (age, max_age_s)
    return True, "fresh — automations ran %.0fs ago" % age


_ha_down_streak = 0


def check_ha_heartbeat():
    """Poll HA's automation-driven heartbeat over the apps network (Bearer token).

    Empty HA_URL/HA_TOKEN -> disabled (stays up), like check_n8n.

    Hysteresis (HA_CONSECUTIVE, like check_cpu_throttle): a planned redeploy takes HA's REST
    API unreachable for ~120s and then leaves the automation scheduler a beat behind, so a
    single cycle can read unreachable OR stale — a transient that should NOT page. Only the
    HA_CONSECUTIVE'th consecutive down cycle pushes `down`; earlier ones push `up` with a
    "streak n/N" msg, and one fresh read resets the streak. A genuinely wedged or auth-broken
    HA stays bad across cycles and still pages. The unreachable-API exception is caught HERE
    (not left to run_once) so the recreate-window connection error rides the same grace as
    staleness — both are the deploy, not a wedge.
    """
    global _ha_down_streak
    if not HA_URL or not HA_TOKEN:
        return True, "HA heartbeat monitoring disabled (no URL/token)"
    try:
        state = _get_json(HA_URL + "/api/states/" + HA_HEARTBEAT_ENTITY,
                          headers={"Authorization": "Bearer " + HA_TOKEN})
        ok, msg = ha_heartbeat_fresh(state, HA_HEARTBEAT_MAX_AGE_S)
    except Exception as e:  # unreachable/auth -> route through the streak, don't page yet
        ok, msg = False, "HA API unreachable: %s" % e
    if ok:
        _ha_down_streak = 0
        return True, msg
    _ha_down_streak += 1
    if _ha_down_streak < HA_CONSECUTIVE:
        return True, "down streak %d/%d (deploy/restart grace): %s" % (
            _ha_down_streak, HA_CONSECUTIVE, msg)
    return False, "%s (%d cycles)" % (msg, _ha_down_streak)


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
    ("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n),
    ("gitops_alive",  _env("KUMA_PUSH_GITOPS_ALIVE",  ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    ("restore_drill", _env("KUMA_PUSH_RESTORE_DRILL", ""), check_restore_drill),
    ("b2_usage",      _env("KUMA_PUSH_B2",            ""), check_b2_usage),
    ("scrutiny",      _env("KUMA_PUSH_SCRUTINY",      ""), check_scrutiny),
    ("pi_pressure",   _env("KUMA_PUSH_PI",            ""), check_pi_pressure),
    ("ha_heartbeat",  _env("KUMA_PUSH_HA",            ""), check_ha_heartbeat),
    ("renovate_alive", _env("KUMA_PUSH_RENOVATE_ALIVE", ""), check_renovate_alive),
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


def touch_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write("%s\n" % time.time())
    except OSError as e:  # best-effort like push(); never crash the loop
        log("WARN: heartbeat write failed:", e)


def main():
    once = "--once" in sys.argv
    log("monitor-bridge starting (interval=%ss, once=%s)" % (INTERVAL, once))
    while True:
        run_once()
        touch_heartbeat()
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
