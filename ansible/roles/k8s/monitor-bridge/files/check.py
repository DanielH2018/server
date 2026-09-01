#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import base64
import json
import os
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# A name the test suite patches is read QUALIFIED from the module that binds it — `cfg.X`
# for every threshold and URL, `bridge_common.log`/`bridge_common.touch_heartbeat` — never
# from-imported. A from-import copies the value into this module's globals at import time,
# so a later `monkeypatch.setattr(bridge_config, "X", ...)` would change nothing this file
# reads and the test would pass against the real value. `_env`/`sanitize` and the verdicts
# are imported by name because the tests patch them HERE, on `check`, where they are read.
# Enforced by ansible/tests/test_bridge_patch_boundary.py; the census of what is patched
# where is ansible/tests/test_monitor_bridge_modules.py.
import bridge_common
from bridge_common import HTTP_TIMEOUT, _env, sanitize
from bridge_parsing import (
    FETCH_BODY_MAX,
    describe_fetch_failure,
    endpoint_label,
    parse_duration,
)
from verdicts_cluster import (
    cadvisor_coverage_shortfall,
    extended_resource_verdict,
    k8s_workloads_verdict,
    ksm_resource_label,
    log_error_verdict,
    targets_verdict,
)
from verdicts_host import (
    hwmon_included_series,
    hwmon_name_maps,
    hwmon_temp_limits,
    hwmon_temp_verdict,
    pi_ports_verdict,
    pi_pressure,
    scrutiny_device_wear,
    scrutiny_freshness,
    scrutiny_health,
    scrutiny_wear_verdict,
    ups_health,
)
from verdicts_service import (
    discord_webhook_ok,
    gitops_alive,
    ha_ban_verdict,
    ha_heartbeat_fresh,
    indexers_down,
    loki_ingestion_fresh,
    n8n_update_streaks,
    n8n_verdict,
    promtail_dropped,
    queue_warnings,
)


import bridge_config as cfg


# Per-check mutable state. The thresholds these pair with moved to bridge_config.py; the
# counters stay beside the code that mutates them.
_n8n_streaks = {}
# Keyed per check, exactly like _host_origin_streaks. NOT one shared counter: all three checks run
# in the same cycle, so a single counter would take three increments per cycle and blow through
# CADVISOR_CONSECUTIVE inside the first one — hysteresis that silently does nothing.
_cadvisor_streaks: dict[str, int] = {}


def origin_sel(*matchers):
    """A `{...}` label-matcher block: the given matchers plus the origin pin, when one applies.

    Returns "" when there is nothing to select on, so `"up%s" % origin_sel()` is a bare `up`
    against the Docker Prometheus and `up{origin="daniel-server"}` against the cluster copy.
    """
    parts = [m for m in matchers if m]
    if cfg.PROM_ORIGIN:
        parts.append(cfg.PROM_ORIGIN)
    return "{%s}" % ", ".join(parts) if parts else ""


def cadvisor_sel(*matchers):
    """A `{...}` block for cAdvisor series, which carry NO origin label — so no origin pin.

    DECIDED: cAdvisor metrics must NOT go through origin_sel(). `origin` is applied by exactly
    one relabel rule, on the `node` job (claude-otel/templates/prometheus.yaml.j2:202); the
    kubernetes-cadvisor job has none. PromQL does not match an absent label, so an origin-pinned
    cAdvisor query selects the empty vector and every check built on it reports green forever.

    That is not hypothetical — it is what check_restarts, check_oom and check_cpu did from the
    Phase G retarget until 2026-08-24. Live at the time of the fix: the unpinned selector matched
    110 cAdvisor series and the pinned form returned `no data`, while the bridge logged
    "OK restarts / OK oom / OK cpu" off empty vectors on every cycle. OOM kills and sustained CFS
    throttling had no other alert path, so both were unmonitored outright.

    The Docker cAdvisor these checks once shared with the cluster copy retired 2026-08-14, so
    there is no longer a second estate for a pin to disambiguate. Use origin_sel() for series
    that genuinely carry the label — `up`, and the node-exporter families behind check_disk and
    check_mem — and this for anything cAdvisor emits.
    """
    parts = [m for m in matchers if m]
    return "{%s}" % ", ".join(parts) if parts else ""


def host_metric_sel(*matchers):
    """A `{...}` block for the HOST-level node_* checks, minus origins owned by another check.

    node_* is estate-wide the moment a host runs node-exporter, so check_disk and check_mem
    scan whatever reports. daniel-pi joined that set when its exporter landed — and
    check_pi_pressure already owns Pi disk and memory, with thresholds written for a 456 MB
    box rather than the 90% that suits the two x86 hosts. Without this exclusion the Pi's
    ordinary working state pages twice for one fact, which is exactly the duplication
    check_mem avoids elsewhere by naming check_oom the single source of truth.

    A regex matcher, so HOST_METRIC_ORIGIN_EXCLUDE can carry a `a|b` list. Series with no
    `origin` label at all are KEPT: Prometheus reads a missing label as "", which `!~` on a
    named host does not match.

    DECIDED: an EXCLUDE, never origin_sel(). cadvisor_sel's note points at "the node-exporter
    families behind check_disk and check_mem" as series that genuinely carry `origin`, which
    reads like an invitation to pin them with origin_sel() — do not. PROM_ORIGIN resolves to
    `origin="daniel-server"` whenever PROM_URL equals CLUSTER_PROM_URL, which the deployed
    env-secret makes true. Pinning these two checks to one host would hide daniel-box's disk
    and memory behind two green tiles, which is precisely the fault HOST_ORIGINS_MIN was added
    for on 2026-08-23. Naming who is OUT keeps every other host in by default.
    """
    parts = [m for m in matchers if m]
    if cfg.HOST_METRIC_ORIGIN_EXCLUDE:
        parts.append('origin!~"%s"' % cfg.HOST_METRIC_ORIGIN_EXCLUDE)
    return "{%s}" % ", ".join(parts) if parts else ""


def _origin_name(labels):
    """The host a per-origin series belongs to, for naming an offender in an alert message.

    The Docker Prometheus has no `origin` label at all (external_labels are applied on
    remote-write, never to local queries), so an empty one means "the only host there is".
    """
    return labels.get("origin") or "host"


# HTTP / parsing helpers (pure-ish, unit-tested)


def _get_json(url, headers=None):
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers is not None:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (internal URLs)
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # Re-raise the SAME type: check_discord branches on `e.code`, so wrapping this would
        # silently turn a decisive 404 (webhook revoked) into a generic "unreachable" that
        # rides the retry streak instead of paging.
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        # str(HTTPError) already leads with "HTTP Error <code>:", so this contributes the
        # endpoint and the server's own explanation, not the status again.
        detail = " ".join((body or "").split())[:FETCH_BODY_MAX]
        e.msg = "%s: %s" % (endpoint_label(url), detail or e.msg)
        raise
    except Exception as e:
        raise RuntimeError(describe_fetch_failure(url, e)) from e


def _post_json(url, payload, headers=None):
    """POST a JSON body and return the parsed JSON response. Same failure contract as _get_json.

    Only the Cloudflare GraphQL endpoint needs this — every other source here is a GET.
    """
    hdrs = {"User-Agent": "monitor-bridge", "Content-Type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = " ".join(e.read().decode("utf-8", "replace").split())
        except Exception:
            detail = ""
        e.msg = "%s: %s" % (endpoint_label(url), detail[:FETCH_BODY_MAX] or e.msg)
        raise
    except Exception as e:
        raise RuntimeError(describe_fetch_failure(url, e)) from e


def _instant_query(base_url, path, query, source):
    """Run an instant query against `base_url + path` (Prometheus or Loki — same
    /query?query= shape and {status, data.result} envelope); return the result list.
    Raises RuntimeError if the endpoint reports a non-success status. `source` labels
    the error ('prometheus'/'loki')."""
    url = base_url + path + "?" + urllib.parse.urlencode({"query": query})
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError("%s query status=%s" % (source, data.get("status")))
    return data.get("data", {}).get("result", [])


def prom_scalar(promql, base=None, source="prometheus"):
    """Run an instant query; return the first result's value as float, or None if empty.

    `base` selects which Prometheus. PROM_URL is the default and is what every PROM_DEPENDENT
    check reads; CLUSTER_PROM_URL is what the CLUSTER_DEPENDENT ones read, under a reachability
    gate of their own — see check_k8s_workloads. Since the Docker plane retired (2026-08-14)
    both env vars render to the same cluster Service URL, so the two gates watch one instance;
    the split is kept so a second Prometheus can be reintroduced without moving every caller.
    Pick the base by which gate is meant to watch the check, not by which host answers.
    """
    result = _instant_query(base or cfg.PROM_URL, "/api/v1/query", promql, source)
    if not result:
        return None
    return float(result[0]["value"][1])


def prom_vector(promql, base=None, source="prometheus"):
    """Run an instant query; return [(labels: dict, value: float), ...] (empty if none).

    Unlike prom_scalar this keeps each series' labels, so checks can name *which*
    container / target / route is failing.
    """
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in _instant_query(
            base or cfg.PROM_URL, "/api/v1/query", promql, source
        )
    ]


_host_origin_streaks: dict[str, int] = {}


def _host_origin_shortfall(key, vec, what, min_origins=None, consecutive=None):
    """(ok, msg) when `vec` covers fewer than `min_origins` hosts, else None.

    Passes (green, but says so) while the shortfall is younger than `consecutive` cycles, so a
    reboot doesn't page; fails once it persists. Any full-coverage cycle resets. `key` separates
    the streaks so disk, memory and host temperature age independently.

    Both thresholds are PARAMETERS defaulting to the shared globals, not reads of the globals.
    check_host_temp needs a different floor from disk and memory — every host declares hwmon
    sensors, where a mountpoint need not exist everywhere — and the 2026-08-29 review M-9
    proposal added an env key that nothing read, leaving hwmon on the shared floor of 2 while
    reading as new coverage. A caller that wants a different floor passes one here; nothing
    reaches for a global whose name it happens to know.
    """
    floor = cfg.HOST_ORIGINS_MIN if min_origins is None else min_origins
    grace = cfg.HOST_ORIGINS_CONSECUTIVE if consecutive is None else consecutive
    origins = {_origin_name(labels) for labels, _ in vec}
    if len(origins) >= floor:
        _host_origin_streaks[key] = 0
        return None
    streak = _host_origin_streaks.get(key, 0) + 1
    _host_origin_streaks[key] = streak
    seen = ", ".join(sorted(origins)) or "none"
    if streak < grace:
        return (
            True,
            "%s: only %d of %d hosts reporting (%s), cycle %d/%d — node rebooting?"
            % (
                what,
                len(origins),
                floor,
                seen,
                streak,
                grace,
            ),
        )
    return (
        False,
        "%s UNKNOWN: only %d of %d hosts reporting (%s) — the absent host is NOT being checked"
        % (
            what,
            len(origins),
            floor,
            seen,
        ),
    )


# checks: each returns (ok, msg)


def check_disk():
    # Percentage computed per series, then grouped by origin, so avail and size always come from
    # the SAME host and device. The previous form took max(avail) and max(size) as two separate
    # queries: fine while one estate reported, but once daniel-server and daniel-box both landed
    # in this Prometheus it paired one host's avail with the other's size, and a filling disk on
    # the smaller host produced an arbitrarily wrong percentage rather than a high one.
    breaching = []
    shortfalls = []
    for mp in cfg.DISK_MOUNTPOINTS:
        sel = host_metric_sel('mountpoint="%s"' % mp)
        vec = prom_vector(
            "max by (origin) (100 * (1 - node_filesystem_avail_bytes%s"
            " / node_filesystem_size_bytes%s))" % (sel, sel)
        )
        if not vec:
            return False, "metric unavailable for %s" % mp
        # Collected, not returned, so a host that IS reporting and IS full still pages ahead of
        # the coverage complaint — a real breach on the survivor outranks the absent host.
        short = _host_origin_shortfall("disk:%s" % mp, vec, "disk %s" % mp)
        if short is not None:
            shortfalls.append(short)
        for labels, used_pct in vec:
            if used_pct > cfg.DISK_MAX_PCT:
                breaching.append("%s %s %.0f%%" % (_origin_name(labels), mp, used_pct))
    if breaching:
        return False, "disk over %.0f%%: %s" % (cfg.DISK_MAX_PCT, ", ".join(breaching))
    failed = [s for s in shortfalls if not s[0]]
    if failed:
        return False, "; ".join(msg for _, msg in failed)
    if shortfalls:
        return True, "; ".join(msg for _, msg in shortfalls)
    return True, "all mounts under %.0f%%" % cfg.DISK_MAX_PCT


def check_cert():
    days = prom_scalar("(min(traefik_tls_certs_not_after) - time()) / 86400")
    if days is None:
        return False, "cert metric unavailable"
    if days < cfg.CERT_MIN_DAYS:
        return False, "cert expires in %.1fd (< %.0fd)" % (days, cfg.CERT_MIN_DAYS)
    return True, "cert valid %.0fd" % days


def check_mem():
    # Host memory pressure only. Per-container OOM kills are reported (with the
    # offending container named) by check_oom — single source of truth.
    #
    # Per-origin for the same reason as check_disk: the bare prom_scalar form took result[0],
    # so which host it reported was an ordering artifact of Prometheus's response once both
    # estates emitted node_memory_*. The division pairs each host's avail with its own total.
    sel = host_metric_sel()
    vec = prom_vector(
        "100 * (1 - node_memory_MemAvailable_bytes%s / node_memory_MemTotal_bytes%s)"
        % (sel, sel)
    )
    if not vec:
        return False, "memory metric unavailable"
    # Computed here, but REPORTED only after the breach scan below — the `if short is not None`
    # return sits under it. Same ordering as check_disk and for the same reason: a reporting host
    # that is actually out of memory outranks a complaint about the absent one. The comment used
    # to say "evaluated after", describing a line position this call has never had (2026-08-23b
    # review L9); what is deferred is the return, not the evaluation.
    short = _host_origin_shortfall("mem", vec, "memory")
    breaching = [
        "%s %.0f%%" % (_origin_name(labels), pct)
        for labels, pct in vec
        if pct > cfg.MEM_MAX_PCT
    ]
    if breaching:
        return False, "mem over %.0f%%: %s" % (cfg.MEM_MAX_PCT, ", ".join(breaching))
    if short is not None:
        return short
    worst = max(pct for _, pct in vec)
    return True, "mem %.0f%%" % worst


def _top_offenders(vector, label, predicate):
    """Names (by `label`) of series matching predicate(value), sorted by value desc."""
    hits = [(m.get(label, "?"), v) for m, v in vector if predicate(v)]
    hits.sort(key=lambda nv: -nv[1])
    return hits


def _cadvisor_blind(key, vec, what):
    """(ok, msg) when `vec` covers too few pods for an offender filter to mean anything, else None.

    Called on the PRE-FILTER vector, which is why each check hands in the one it already fetched.
    Holds `up` (saying so) while the shortfall is younger than CADVISOR_CONSECUTIVE cycles, then
    fails. Any covered cycle resets that check's streak.
    """
    msg = cadvisor_coverage_shortfall(len(vec), cfg.CADVISOR_PODS_MIN, what)
    if msg is None:
        _cadvisor_streaks[key] = 0
        return None
    _cadvisor_streaks[key], ok, out = down_streak(
        _cadvisor_streaks.get(key, 0),
        cfg.CADVISOR_CONSECUTIVE,
        msg,
        "kubelet restart grace",
        held_label="cAdvisor coverage shortfall",
    )
    return ok, out


def check_restarts():
    """Containers restarting more than RESTART_MAX times within RESTART_WINDOW.

    Catches crash-loops that an intermittent up-check can miss.
    """
    vec = prom_vector(
        "sum by (pod) (changes(container_start_time_seconds%s[%s]))"
        % (cadvisor_sel('container!=""', 'container!="POD"'), cfg.RESTART_WINDOW)
    )
    blind = _cadvisor_blind("restarts", vec, "restart loops")
    if blind is not None:
        return blind
    offenders = _top_offenders(vec, "pod", lambda v: v > cfg.RESTART_MAX)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) restarting >%.0fx in %s: %s" % (
            len(offenders),
            cfg.RESTART_MAX,
            cfg.RESTART_WINDOW,
            desc,
        )
    return True, "no restart loops in %s" % cfg.RESTART_WINDOW


def check_oom():
    """Containers OOM-killed within OOM_WINDOW, naming each one.

    Closes the loop on the per-container memory limits (deploy.resources). An empty vector used to
    read as green here, which is how OOM kills went unmonitored for the whole Phase G window;
    _cadvisor_blind now reports that as UNKNOWN.
    """
    vec = prom_vector(
        "sum(increase(container_oom_events_total%s[%s])) by (pod)"
        % (cadvisor_sel('container!=""', 'container!="POD"'), cfg.OOM_WINDOW)
    )
    blind = _cadvisor_blind("oom", vec, "OOM kills")
    if blind is not None:
        return blind
    offenders = _top_offenders(vec, "pod", lambda v: v > 0)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) OOM-killed in %s: %s" % (
            len(offenders),
            cfg.OOM_WINDOW,
            desc,
        )
    return True, "no OOM kills in %s" % cfg.OOM_WINDOW


# Kept as its own module int rather than folded into _down_streaks below: its down branch
# is bespoke (the page message embeds the throttle thresholds), unlike the other four
# checks, which all call the shared down_streak() helper. See down_streak()'s docstring.
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
    and are ignored. Absent cfs metrics used to empty both queries and read green; the floor in
    _cadvisor_blind reports that as UNKNOWN instead. Its vector is the smallest of the three —
    only pods carrying a cpu limit — which is what set the shared floor.

    On top of the two gates, CPU_CONSECUTIVE adds hysteresis: only the Nth consecutive
    breaching cycle goes `down` (~(N×INTERVAL)s of continuous throttling at the loop
    cadence). One- or two-cycle bursts — flaresolverr solving a challenge, homepage
    briefly hugging the cores floor — push `up` with the offender named in the msg, so
    the evidence stays in the bridge log without paging. A clean cycle resets the streak.
    """
    global _cpu_breach_streak
    sel = cadvisor_sel('container!=""', 'container!="POD"')
    ratio_vec = prom_vector(
        "sum(rate(container_cpu_cfs_throttled_periods_total%s[%s])) by (pod) "
        "/ sum(rate(container_cpu_cfs_periods_total%s[%s])) by (pod)"
        % (sel, cfg.CPU_WINDOW, sel, cfg.CPU_WINDOW)
    )
    blind = _cadvisor_blind("cpu", ratio_vec, "CPU throttling")
    if blind is not None:
        _cpu_breach_streak = 0
        return blind
    lost_cores = dict(
        (m.get("pod", "?"), v)
        for m, v in prom_vector(
            "sum(rate(container_cpu_cfs_throttled_seconds_total%s[%s])) by (pod)"
            % (sel, cfg.CPU_WINDOW)
        )
    )
    threshold = cfg.CPU_THROTTLE_PCT / 100.0
    offenders = []
    for m, ratio in ratio_vec:
        name = m.get("pod", "?")
        lost = lost_cores.get(name, 0.0)
        if ratio > threshold and lost > cfg.CPU_MIN_THROTTLED_CORES:
            offenders.append((name, ratio, lost))
    offenders.sort(key=lambda nrl: -nrl[1])
    if not offenders:
        _cpu_breach_streak = 0
        return True, "no sustained CPU throttling in %s" % cfg.CPU_WINDOW
    _cpu_breach_streak += 1
    desc = ", ".join(
        "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5]
    )
    if _cpu_breach_streak < cfg.CPU_CONSECUTIVE:
        return True, "throttling streak %d/%d (not alerting yet): %s" % (
            _cpu_breach_streak,
            cfg.CPU_CONSECUTIVE,
            desc,
        )
    return (
        False,
        "%d container(s) CPU-throttled >%.0f%% & >%.2f cores for %d cycles: %s"
        % (
            len(offenders),
            cfg.CPU_THROTTLE_PCT,
            cfg.CPU_MIN_THROTTLED_CORES,
            _cpu_breach_streak,
            desc,
        ),
    )


def check_prometheus():
    """Is Prometheus itself reachable and answering queries?

    A trivial `vector(1)` instant query returns 1.0 whenever Prometheus is up; if it's
    down/unreachable prom_scalar raises (connection error) and run_once renders this monitor
    `down` with the error. This is the single root-cause signal for the prom-dependent checks:
    run_once probes it FIRST each cycle and, when it's down, SUPPRESSES the metric checks (which
    would otherwise all fail at once — one outage, a storm of identical pages) so only this
    monitor alerts. A single scrape target being down (Prometheus up, one exporter gone) still
    surfaces separately on the Scrape Targets monitor — a distinct condition from this one.
    """
    val = prom_scalar("vector(1)")
    if val is None:
        return False, "Prometheus answered but returned no data for vector(1)"
    return True, "Prometheus reachable"


def check_targets_down():
    """Any Prometheus scrape target reporting up==0 (monitoring going blind)."""
    return targets_verdict(prom_vector("up%s" % origin_sel()), cfg.TARGETS_MIN)


def check_traefik_5xx():
    """Elevated 5xx ratio per Traefik service, naming each offender.

    Per-service (not aggregate) for two reasons: the alert points at *which* backend is
    erroring, and a broken low-traffic service can't hide diluted below the threshold by
    healthy high-traffic ones. The TRAEFIK_MIN_RPS floor is per-service too — same idea
    as before, a single error on a near-idle route is not a 100%-error-ratio alarm.
    """
    total_vec = prom_vector(
        "sum(rate(traefik_service_requests_total[5m])) by (service)"
    )
    err_rps = dict(
        (m.get("service", "?"), v)
        for m, v in prom_vector(
            'sum(rate(traefik_service_requests_total{code=~"5.."}[5m])) by (service)'
        )
    )
    offenders = []
    total_rps = 0.0
    eligible = 0
    for m, rps in total_vec:
        total_rps += rps
        if rps < cfg.TRAEFIK_MIN_RPS:
            continue
        eligible += 1
        svc = m.get("service", "?")
        pct = 100.0 * err_rps.get(svc, 0.0) / rps
        if pct > cfg.TRAEFIK_5XX_PCT:
            offenders.append((svc, pct, rps))
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return False, "%d service(s) over %.0f%% 5xx: %s" % (
            len(offenders),
            cfg.TRAEFIK_5XX_PCT,
            desc,
        )
    return True, "5xx ok: %d service(s) above floor, %.2f rps total" % (
        eligible,
        total_rps,
    )


def check_traefik_latency():
    """Share of slow requests per Traefik service, naming each offender.

    The gap check_traefik_5xx cannot close: a slow route still answers 200, so an error-ratio
    check stays green while a service is unusable. Nothing here watched latency at all before
    this, so a backend could degrade indefinitely without any monitor noticing.

    Latency rather than the 499s that slow routes produce: a 499 only means the client
    disconnected first, which is also what happens every time someone closes a tab with
    in-flight requests. On a polling dashboard that is constant and entirely normal, so a
    499-based alert would page for ordinary browsing.

    Same shape as check_traefik_5xx deliberately: per-service so the alert names the offender,
    and behind the same TRAEFIK_MIN_RPS floor so one slow request on a near-idle route is not
    an alarm. It shares the 5xx check's ratio form too, for the reason in TRAEFIK_SLOW_BUCKET:
    a share of requests past a real bucket edge is exact where an interpolated quantile is not.

    Both rates come from the histogram's own series (_count, not traefik_service_requests_total)
    so numerator and denominator are always the same scrape of the same metric family — mixing
    the two counters lets a ratio drift outside 0-100% between scrapes.
    """
    total = dict(
        (m.get("service", "?"), v)
        for m, v in prom_vector(
            "sum(rate(traefik_service_request_duration_seconds_count[5m])) by (service)"
        )
    )
    under = dict(
        (m.get("service", "?"), v)
        for m, v in prom_vector(
            'sum(rate(traefik_service_request_duration_seconds_bucket{le="%s"}[5m])) '
            "by (service)" % cfg.TRAEFIK_SLOW_BUCKET
        )
    )
    offenders = []
    unmeasurable = []
    eligible = 0
    worst = 0.0
    for svc, rps in total.items():
        if rps < cfg.TRAEFIK_MIN_RPS:
            continue
        # A cumulative histogram emits every bucket for any service that served a request, so a
        # service missing from `under` means the le= selected nothing at all — Traefik's buckets
        # were reconfigured out from under this check. Report that rather than reading the
        # absent series as 0 requests under the boundary, which would page every service at once.
        if svc not in under:
            unmeasurable.append(svc)
            continue
        eligible += 1
        pct = 100.0 * (1.0 - under[svc] / rps)
        worst = max(worst, pct)
        if pct > cfg.TRAEFIK_SLOW_PCT:
            offenders.append((svc, pct, rps))
    if unmeasurable:
        return (
            False,
            "no %ss bucket for %d service(s) (%s) — check Traefik's histogram buckets"
            % (
                cfg.TRAEFIK_SLOW_BUCKET,
                len(unmeasurable),
                ", ".join(sorted(unmeasurable)[:5]),
            ),
        )
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return (
            False,
            "%d service(s) with over %.0f%% of requests slower than %ss: %s"
            % (
                len(offenders),
                cfg.TRAEFIK_SLOW_PCT,
                cfg.TRAEFIK_SLOW_BUCKET,
                desc,
            ),
        )
    return True, "latency ok: %d service(s) above floor, worst %.1f%% over %ss" % (
        eligible,
        worst,
        cfg.TRAEFIK_SLOW_BUCKET,
    )


def _parse_behind(marker):
    """Split the deployer's "<origin_sha> <unix_ts_first_seen>" marker. Returns (sha, since) with
    since=None when absent or unparseable — an unreadable marker must read as "not behind" rather
    than page forever on garbage."""
    if not marker:
        return "", None
    parts = marker.split()
    if len(parts) != 2:
        return "", None
    try:
        return parts[0], float(parts[1])
    except ValueError:
        return "", None


def gitops_status(
    hold_sha,
    diverged_sha=None,
    behind_since=None,
    now=None,
    max_behind_s=cfg.GITOPS_BEHIND_MAX_S,
    hold_plane=None,
):
    """Pure: is the deploy pipeline in a state needing operator action? Returns (ok, msg).

    Three down states share this monitor, most-specific first: a rolled-back commit HELD pending a
    revert, a local↔origin DIVERGENCE where the deployer can't fast-forward and silently noops
    forever while origin's new commits never deploy (2026-07-15 review L3), and the host simply
    sitting BEHIND origin for too long.

    Behind-ness is the general case the other two are specific instances of, and it is the one that
    caught nothing before: a deferred BROAD change never fast-forwards, so the host parks on an old
    tree while last_run keeps ticking (Alive green) and is_diverged stays false (origin is a strict
    descendant, so Status green too). daniel-server ran a 12-commit-old tree for hours that way on
    2026-08-02, all signals green, until un-deployed DNS records were noticed by hand.

    It is age-gated because being behind is normal in the small: a push is behind for one tick, and
    the dirty-tree path is behind for a whole edit session by design. Only sustained behind-ness is
    a fault. hold/diverged are still reported ahead of it — they name the actual cause, where
    "behind" only names the symptom.
    """
    if hold_sha:
        # A held BROAD apply is a different fault with a different fix. That arm is
        # forward-only: the tree is already fast-forwarded and a plane playbook failed
        # partway, so reverting the PR undoes nothing and the operator has to fix forward
        # and re-run. hold_sha still decides whether we page — hold_plane only says which
        # sentence to print, so a stale marker left by a cleared hold cannot page alone.
        if hold_plane:
            return False, (
                "broad apply held at %s — %s failed, plane unapplied; "
                "fix forward and re-run it" % (hold_sha[:8], hold_plane)
            )
        return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
    if diverged_sha:
        return False, (
            "local diverged from origin at %s — deployer can't fast-forward, new commits "
            "aren't deploying; reconcile the host tree" % diverged_sha[:8]
        )
    sha, since = _parse_behind(behind_since)
    if since is not None:
        age_s = (time.time() if now is None else now) - since
        if age_s > max_behind_s:
            return False, (
                "host %.0fh behind origin at %s (> %.0fh) — deploy deferred (broad change / "
                "dirty tree); run the manual deploy on the host"
                % (age_s / 3600, sha[:8], max_behind_s / 3600)
            )
    return True, "no held deploy"


def check_n8n():
    """Consecutive failures of active ("Prod") n8n workflows (streak accumulated across cycles).

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia). n8n
    doesn't save successful executions, so the per-workflow failure streak lives in the
    module-global _n8n_streaks and is advanced by n8n_update_streaks each cycle; n8n_verdict
    turns it into the page decision. Empty N8N_API_KEY -> disabled (stays up) so it never
    false-pages before the operator sets the key. An unreachable/erroring API raises -> the loop
    renders it down with the error, like check_targets_down (a dead API surfaces, not silent-green).
    """
    if not cfg.N8N_API_KEY:
        return True, "n8n monitoring disabled (no API key)"
    headers = {"X-N8N-API-KEY": cfg.N8N_API_KEY}
    workflows = _get_json(
        cfg.N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers
    )
    executions = _get_json(
        cfg.N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers
    )
    streaks = n8n_update_streaks(
        workflows,
        executions,
        _n8n_streaks,
        datetime.now(timezone.utc),
        parse_duration(cfg.N8N_FAIL_WINDOW),
    )
    return n8n_verdict(
        streaks, cfg.N8N_CONSECUTIVE_MAX, cfg.N8N_SYSTEMIC_STREAK, cfg.N8N_SYSTEMIC_MAX
    )


def check_arr_queue():
    """Sonarr/Radarr queue warning/blocked-import watchdog (see queue_warnings).

    Empty SONARR_API_KEY/RADARR_API_KEY independently skip that app (like the multi-webhook
    Discord check); both empty -> disabled (stays up), like check_n8n. An unreachable *arr
    API is NOT caught here — it bubbles up and _evaluate renders it `down` with the error,
    the same convention as check_n8n/check_scrutiny (a dead dependency pages; there's no
    shared root cause here the way Prometheus/exporter outages have, so nothing to gate).
    pageSize=250 mirrors n8n's page cap — ample for a homelab queue.
    """
    apps = [
        (
            "Sonarr",
            cfg.SONARR_URL
            + "/api/v3/queue?includeUnknownSeriesItems=true&pageSize=250",
            cfg.SONARR_API_KEY,
        ),
        (
            "Radarr",
            # includeUnknownMovieItems is Radarr's spelling of Sonarr's
            # includeUnknownSeriesItems — both default FALSE, hiding exactly the unmapped/
            # poisoned-release queue items this check exists for (2026-07-01 incident class).
            cfg.RADARR_URL + "/api/v3/queue?includeUnknownMovieItems=true&pageSize=250",
            cfg.RADARR_API_KEY,
        ),
    ]
    configured = [a for a in apps if a[2]]
    if not configured:
        return True, "arr queue monitoring disabled (no API keys)"
    offenders = []
    for app_name, url, api_key in configured:
        data = _get_json(url, headers={"X-Api-Key": api_key})
        offenders.extend(queue_warnings(data, app_name))
    if offenders:
        desc = "; ".join(
            "[%s] %s — %s" % (app, sanitize(title), sanitize(reason))
            for app, title, reason in offenders[:5]
        )
        return False, "%d queue item(s) need review: %s" % (len(offenders), desc)
    return True, "queue clean (%s)" % ", ".join(a[0] for a in configured)


def bazarr_problems(status, health):
    """Problems from Bazarr's /api/system/status and /api/system/health payloads.

    Pure, so the reject case is testable without a live Bazarr.

    The peer-version fields are the interesting half. Bazarr fills `sonarr_version` /
    `radarr_version` by calling each app with ITS OWN stored copy of that app's API key, so a
    key Bazarr no longer holds correctly leaves the field empty while everything else about
    Bazarr still looks healthy. Measured against the live app 2026-08-29 after the keys were
    fixed: `sonarr_version='4.0.17.2952'`, `radarr_version='6.1.1.10360'`.

    An ABSENT field is not the same as an empty one and is deliberately ignored: Bazarr omits
    the key entirely when that integration is switched off, and alerting on a peer the operator
    turned off would page forever. Empty-but-present is the broken case.
    """
    problems = []
    data = (status or {}).get("data") or {}
    for peer in ("sonarr", "radarr"):
        field = "%s_version" % peer
        if field not in data:
            continue
        if not str(data.get(field) or "").strip():
            problems.append(
                "bazarr cannot reach %s (empty %s — stale API key in bazarr's own config?)"
                % (peer, field)
            )
    for item in (health or {}).get("data") or []:
        problems.append(
            "%s: %s" % (sanitize(item.get("object")), sanitize(item.get("issue")))
        )
    return problems


def check_bazarr():
    """Bazarr's own health, and whether it can still talk to Sonarr and Radarr.

    Bazarr is the one *arr with no exporter, and that is why the 2026-08-29 stale-key incident
    surfaced only as an OOM 90 minutes later. Sonarr's and Radarr's own stale keys showed up
    immediately as failing exportarr scrapes; Bazarr had nothing watching it.

    NOT an exportarr sidecar, deliberately. exportarr does speak bazarr, but at the pinned
    v2.3.0 its collector always performs the full episode-subtitle walk — upstream measures
    that in "tens of seconds", spent inside Bazarr — and v2.3.0 predates the
    overlapping-collection skip that upstream added specifically to stop concurrent walks
    stacking (their issue #380, "bazarr CPU drainage"). Pointing that at the workload that had
    just OOM-killed would risk causing the failure this exists to detect. These two endpoints
    cost 477 and 13 bytes and measured 2-7 ms over three runs each, 2026-08-29.

    Empty BAZARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Bazarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error, the
    check_arr_queue/check_prowlarr_indexers convention. That covers the 401 a wrong key
    returns, which is itself the signal that Bazarr's API key in SOPS has gone stale.
    """
    if not cfg.BAZARR_API_KEY:
        return True, "bazarr monitoring disabled (no API key)"
    headers = {"X-API-KEY": cfg.BAZARR_API_KEY}
    status = _get_json(cfg.BAZARR_URL + "/api/system/status", headers=headers)
    health = _get_json(cfg.BAZARR_URL + "/api/system/health", headers=headers)
    problems = bazarr_problems(status, health)
    if problems:
        return False, "; ".join(problems[:5])
    versions = (status or {}).get("data") or {}
    return True, "bazarr ok (sonarr %s, radarr %s)" % (
        versions.get("sonarr_version") or "n/a",
        versions.get("radarr_version") or "n/a",
    )


def check_prowlarr_indexers():
    """Prowlarr sustained-indexer watchdog (see indexers_down): page only when an indexer has been
    failing >= PROWLARR_INDEXER_MIN_DOWN_MIN, not on the brief flaps public trackers throw that
    self-clear inside Prowlarr's backoff.

    Empty PROWLARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Prowlarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error (the
    check_arr_queue/check_n8n convention; the sustained-failure grace is about indexer flaps, not
    the bridge's own reach). The all-indexers-down red error stays with Prowlarr's own in-app
    onHealthIssue notification — this owns the per-indexer sustained signal Prowlarr can't express.
    """
    if not cfg.PROWLARR_API_KEY:
        return True, "prowlarr indexer monitoring disabled (no API key)"
    headers = {"X-Api-Key": cfg.PROWLARR_API_KEY}
    status = _get_json(cfg.PROWLARR_URL + "/api/v1/indexerstatus", headers=headers)
    indexers = _get_json(cfg.PROWLARR_URL + "/api/v1/indexer", headers=headers)
    name_by_id = {i.get("id"): i.get("name") for i in indexers}
    offenders = indexers_down(
        status,
        name_by_id,
        datetime.now(timezone.utc),
        cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
        cfg.PROWLARR_INDEXER_IGNORE.split(","),
    )
    if offenders:
        desc = "; ".join("%s down %.0fm" % (sanitize(n), m) for n, m in offenders[:5])
        return False, "%d indexer(s) failing >=%gm: %s" % (
            len(offenders),
            cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
            desc,
        )
    return True, "all %d indexer(s) ok (none failing >=%gm)" % (
        len(name_by_id),
        cfg.PROWLARR_INDEXER_MIN_DOWN_MIN,
    )


def check_gitops_alive():
    try:
        with open(os.path.join(cfg.GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, cfg.GITOPS_MAX_AGE_S)


def _read_gitops_marker(name):
    try:
        with open(os.path.join(cfg.GITOPS_STATE_DIR, name)) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def check_gitops_status():
    return gitops_status(
        _read_gitops_marker("hold_sha"),
        _read_gitops_marker("diverged_sha"),
        _read_gitops_marker("behind_since"),
        hold_plane=_read_gitops_marker("hold_plane"),
    )


def check_etcd_restore_drill():
    """Is the off-box etcd snapshot still PROVABLY restorable?

    The snapshot half has been taken, uploaded and alarmed since 2026-08-16. Until 2026-08-28
    nothing watched the restore half: the drill wrote a stamp no code read, so a silently
    failing drill was indistinguishable from a passing one. etcd carries the Longhorn `Backup`
    CRs needed to FIND the volume backups, so this is the tier whose failure voids the rest of
    the recovery chain.

    Reads `last-success-list-only` SPECIFICALLY, never `last-success-full`. Only the list-only
    leg is scheduled — the full drill cannot pass on this host (five structural
    `k3s server --cluster-reset` failures documented in the drill's header) — so accepting either
    file would report the object-graph restore as proven when nothing here has ever proven it.
    That is the "one tier hiding behind another tier's evidence" shape, and the drill writes the
    mode into the stamp precisely so a reader cannot make that mistake.

    Fails closed on all three ways the input can be missing, and they are reported distinctly
    because they need different fixes:
      absent      the drill has never passed here — the state most worth reporting, and the one
                  `[[ -f $STAMP ]] && check_age` would have reported green
      unreadable  the stamp exists but this uid cannot read it. Real, not hypothetical: the
                  first run wrote 0640 root:root under UMASK 027 while this pod runs as uid
                  1000, and an unreadable file is otherwise indistinguishable from an absent one
      unparseable a stamp written by a future version whose format this cannot read
    """
    path = os.path.join(cfg.ETCD_DRILL_STATE_DIR, "last-success-list-only")
    try:
        with open(path) as fh:
            body = fh.read()
    except FileNotFoundError:
        return False, "no etcd restore drill has ever passed (no list-only stamp)"
    except PermissionError:
        return (
            False,
            "etcd drill stamp exists but is unreadable by this uid (needs 0644)",
        )
    except OSError as exc:
        return False, "cannot read the etcd drill stamp: %s" % exc

    epoch = None
    for line in body.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "epoch":
            try:
                epoch = float(value.strip())
            except ValueError:
                epoch = None
            break
    if epoch is None:
        return False, "etcd drill stamp has no readable epoch"

    age_s = time.time() - epoch
    if age_s > cfg.ETCD_DRILL_MAX_AGE_S:
        return (
            False,
            "etcd restore drill last passed %.1f days ago (weekly cadence)"
            % (age_s / 86400),
        )
    return True, "etcd restore drill passed %.1f days ago" % (age_s / 86400)


def scrutiny_wear_devices(summary):
    """One /api/device/<wwn>/details fetch per non-archived device.

    The wear attributes are not in /api/summary, which is what makes this N calls per cycle rather
    than none — same shape as check_k8s_workloads' six Prometheus queries. Each payload is ~19 KB
    and only smart_results[0] is read. A failing fetch raises out of _get_json and the runner
    reports DOWN; that is deliberate and must not be caught here.
    """
    devices = []
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        name = dev.get("device_name") or wwn
        model = dev.get("model_name")
        label = "%s (%s)" % (name, model) if model else name
        details = _get_json("%s/api/device/%s/details" % (cfg.SCRUTINY_URL, wwn))
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny():
    data = _get_json(cfg.SCRUTINY_URL + "/api/summary")
    summary = (data.get("data") or {}).get("summary")
    fresh_ok, fresh_msg = scrutiny_freshness(summary, cfg.SCRUTINY_MAX_AGE_H)
    if not fresh_ok:
        return False, fresh_msg
    health_ok, health_msg = scrutiny_health(summary, cfg.SCRUTINY_TEMP_MAX)
    if not health_ok:
        return False, health_msg
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and wear answers the same question device_status does — is the drive still
    # fit to hold the data on it — just months earlier. Fetched only once freshness passes, so a
    # dead collector costs no per-device calls.
    if not cfg.SCRUTINY_WEAR_MAX:
        return True, "%s; %s" % (fresh_msg, health_msg)
    wear_ok, wear_msg = scrutiny_wear_verdict(
        scrutiny_wear_devices(summary), cfg.SCRUTINY_WEAR_MAX
    )
    if not wear_ok:
        return False, wear_msg
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear_msg)


def check_host_temp():
    """Board and CPU temperature across the three hosts, from node-exporter's hwmon collector.

    Answers the one thermal question nothing else here asks: is a host cooking? A hot box
    throttles, then corrupts, then dies, and every existing monitor reads green throughout —
    check_cpu_throttle sees CFS throttling (a cgroup limit, not heat), and the Grafana
    "Hardware Temperature Monitor" panel plots these series but nobody watches a panel.

    Drives are NOT read here; see HWMON_TEMP_EXCLUDE_CHIP. Two arms assign every remaining
    sensor a limit — its own declared max where that max is plausible, a flat ceiling where it
    is not — so coverage is exhaustive rather than whatever the metric join happens to yield.
    The limit selection is pure and lives in verdicts_host, which is what lets the red-proof
    tests drive it without a Prometheus.

    Empty vector pages rather than passing: no sensors means EVERY collector went blind, and a
    "nothing is too hot" verdict from zero readings is the inert-check failure this repo has
    paid for twice. A PARTIAL blindness — one host gone, the others answering — is what
    HWMON_TEMP_ORIGINS_MIN covers, and the empty-vector arm structurally cannot see it.

    Ordering mirrors check_disk and check_mem: a host that IS reporting and IS too hot pages
    ahead of a complaint about the absent one. The two graces stay separate and are never
    compounded — down_streak is the thermal-spike grace and applies only to the hot-sensor path,
    while the coverage shortfall carries its own hysteresis inside _host_origin_shortfall.
    """
    temps = prom_vector("node_hwmon_temp_celsius")
    # node-exporter keeps the readable names in two side metrics rather than on the reading, so
    # naming the hot sensor `daniel-box k10temp/Tctl` instead of
    # `daniel-box/pci0000:00_0000:00:18_3/temp1` costs two more instant queries. Both are tiny
    # (11 and 16 series live on 2026-09-01) and neither can fail the check: an empty answer just
    # falls back to the sysfs path.
    names = hwmon_name_maps(
        prom_vector("node_hwmon_chip_names"),
        prom_vector("node_hwmon_sensor_label"),
    )
    limits = hwmon_temp_limits(
        temps,
        prom_vector("node_hwmon_temp_max_celsius"),
        cfg.HWMON_TEMP_RATIO,
        cfg.HWMON_TEMP_FALLBACK_C,
        cfg.HWMON_TEMP_MIN_PLAUSIBLE_C,
        cfg.HWMON_TEMP_MAX_PLAUSIBLE_C,
        cfg.HWMON_TEMP_EXCLUDE_CHIP,
        names,
    )
    # Counted over the series that survive exclusion, via the same predicate hwmon_temp_limits
    # uses — a host whose only sensors are excluded is not a host this check covers.
    short = _host_origin_shortfall(
        "host_temp",
        hwmon_included_series(temps, cfg.HWMON_TEMP_EXCLUDE_CHIP),
        "host temperature",
        min_origins=cfg.HWMON_TEMP_ORIGINS_MIN,
        consecutive=cfg.HWMON_TEMP_ORIGINS_CONSECUTIVE,
    )
    ok, msg = hwmon_temp_verdict(limits)
    if not ok:
        _down_streaks["host_temp"], ok, msg = down_streak(
            _down_streaks.get("host_temp", 0),
            cfg.HWMON_TEMP_CONSECUTIVE,
            msg,
            "thermal spike grace",
        )
        return ok, msg
    _down_streaks["host_temp"] = 0
    if short is not None:
        return short
    return True, msg


# Per-check consecutive-down count (check_ups/check_ha_heartbeat/check_discord/
# check_longhorn_volumes), keyed by check name, mutated via down_streak(). Reset to 0 on an
# `ok` result by each check itself, and cleared between tests by conftest.py's autouse
# fixture. Distinct from _grace_streaks below, which is apply_startup_grace's per-name
# state for the reach-out checks' post-reboot startup grace, a different mechanism keyed
# by a disjoint set of names.
_down_streaks: dict[str, int] = {}


def check_ups():
    """UPS battery health from HA's Prometheus-scraped sensors (see the UPS_* env block above).

    Three arms: charge %, estimated runtime, and the replace-battery self-test verdict. All queries
    empty -> disabled (stays up), like check_pi_pressure without a glances URL. Two defer paths keep
    this from double-paging a source outage another monitor already owns:
      - ALL arms absent while HA's scrape is DOWN (or the up-gate is unqueryable) -> HA's whole
        Prometheus scrape is down (Scrape Targets' page). If instead HA is scraping fine (up-gate == 1)
        and the replace arm is configured, all-absent means every UPS entity was renamed/removed at
        once — Scrape Targets can't see it, so page through the streak rather than silently unmonitor.
      - both NUT NUMERIC arms (charge, runtime) absent while the replace-battery arm is still present
        -> the NUT server/integration dropped: HA drops the unavailable numeric sensors, but the
        replace-battery template FLOORS to 0 (stays present) in that same outage (templates.yaml), so
        a NUT outage can't reach the all-absent branch above. The nut pod liveness probe owns
        NUT-server death, so defer rather than double-paging it with a misdirecting "entity renamed?".
    A PARTIAL absence that is NEITHER of those (a single numeric arm gone, or the replace arm gone
    while the numerics report) is a specific entity rename/removal — it pages (through the streak)
    rather than silently monitoring the survivor. UPS_CONSECUTIVE hysteresis (like check_ha_heartbeat)
    rides out a single-cycle runtime dip from a load spike or an HA-restart blip; only a sustained
    problem pages.
    """
    configured = [
        (name, q)
        for name, q in (
            ("charge", cfg.UPS_CHARGE_QUERY),
            ("runtime", cfg.UPS_RUNTIME_QUERY),
            ("replace-battery", cfg.UPS_REPLACE_QUERY),
        )
        if q
    ]
    if not configured:
        return True, "UPS monitoring disabled (no query)"
    values = {name: prom_scalar(q) for name, q in configured}
    if all(v is None for v in values.values()):
        # All arms gone. Usually HA's whole Prometheus scrape is down (the numeric AND the template
        # sensors vanish together) — Scrape Targets owns that, so defer. But if HA is scraping fine and
        # every UPS entity was renamed/removed at once, Scrape Targets can't see it and the UPS would go
        # silently unmonitored — so gate on HA's own up series and fall through to the partial-absence
        # page below when HA is affirmatively up AND the replace arm is configured (its 0-floor in a NUT
        # outage means a real NUT-server outage is never all-absent, so this can't misfire on one).
        # An unqueryable/absent gate keeps the safe defer (never page over a source outage another
        # monitor owns).
        ha_up = prom_scalar(cfg.UPS_HA_UP_QUERY) if cfg.UPS_HA_UP_QUERY else None
        if not (ha_up is not None and ha_up > 0.5 and "replace-battery" in values):
            _down_streaks["ups"] = 0
            return (
                True,
                "no UPS data in Prometheus (HA scrape down? Scrape Targets owns source liveness)",
            )
    missing = [name for name, v in values.items() if v is None]
    if (
        "charge" in values
        and "runtime" in values
        and values["charge"] is None
        and values["runtime"] is None
        and values.get("replace-battery") is not None
    ):
        # NUT server/integration down, NOT an entity rename: charge+runtime are direct NUT numeric
        # sensors HA drops from Prometheus when the source goes unavailable, while the replace-battery
        # arm is an HA template binary_sensor that FLOORS to 0 (stays present) in that same outage
        # (templates.yaml) — so a NUT outage reads as both numeric arms absent + replace present, past
        # the all-absent branch above. The nut pod liveness probe owns NUT-server death, so defer
        # rather than double-paging it through the partial-absence path below with a misdirecting
        # "entity renamed?" msg. A single numeric arm gone (charge XOR runtime) is still a real rename.
        _down_streaks["ups"] = 0
        return (
            True,
            "NUT numeric arms (charge, runtime) absent — NUT server/integration down; "
            "nut healthcheck owns it",
        )
    if missing:
        # Some configured arms present, others absent — NOT the whole-scrape-down case above but a
        # specific entity rename/removal. Don't silently monitor the survivor: passing on the present
        # arm(s) would blind the missing one (e.g. keep charge green while the primary aged-battery
        # runtime signal is gone). Flag it through the same down-streak so an HA-restart blip still
        # gets the UPS_CONSECUTIVE grace, but a sustained partial drop pages.
        ok, msg = (
            False,
            "UPS sensor(s) absent: %s (entity renamed/removed?)" % ", ".join(missing),
        )
    else:
        ok, msg = ups_health(
            values.get("charge"),
            values.get("runtime"),
            values.get("replace-battery"),
            cfg.UPS_CHARGE_MIN_PCT,
            cfg.UPS_RUNTIME_MIN_S,
        )
    if ok:
        _down_streaks["ups"] = 0
        return True, msg
    _down_streaks["ups"], ok, msg = down_streak(
        _down_streaks.get("ups", 0), cfg.UPS_CONSECUTIVE, msg, "grace"
    )
    return ok, msg


def check_pi_pressure():
    """Swap-thrash / overload early warning for the memory-constrained Pi.

    Empty PI_GLANCES_URL -> disabled (stays up), like check_n8n without an API key.
    An unreachable glances raises -> the loop renders it down with the error.
    """
    if not cfg.PI_GLANCES_URL:
        return True, "pi monitoring disabled (no glances URL)"
    load = _get_json(cfg.PI_GLANCES_URL + "/api/4/load")
    mem = _get_json(cfg.PI_GLANCES_URL + "/api/4/mem")
    fs = _get_json(cfg.PI_GLANCES_URL + "/api/4/fs")
    ok, msg = pi_pressure(
        load, mem, fs, cfg.PI_LOAD_MAX, cfg.PI_MEM_MIN_MB, cfg.PI_DISK_MAX_PCT
    )
    return with_pi_ports(ok, msg)


def _tcp_open(host, port, timeout):
    """True when something accepts a TCP connection on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def with_pi_ports(ok, msg):
    """Fold the published-port arm into the Pi verdict, a dead port winning the message.

    Folded into this monitor rather than given its own for the reason recorded at with_ha_ban:
    a new Kuma monitor costs a new push token in SOPS. This monitor already owns "the Pi is
    unhealthy", and a service that stopped listening is that.

    # DECIDED: TCP connect is the primary signal, glances only the attribution. Measured
    # 2026-08-27 against the live Pi: /api/4/load, /mem and /fs answer in 0.03-0.06s each,
    # while /api/4/containers took 4.43s and then TIMED OUT at the 10s HTTP_TIMEOUT on the
    # very next call. Polling it every cycle would have left the arm failing open most of the
    # time — inert behind a green monitor, which is the failure mode this arm exists to
    # catch in the first place. It is also a heavy query to run every cycle against a 456 MB
    # Zero 2 W whose pressure this same check reports.
    # DECIDED: the message leads with the container names when the arm fires, because
    # "pi_pressure DOWN" otherwise pages someone to look at load and memory when the fault is
    # neither. Same shape as with_ha_ban putting the ban first.
    # DECIDED: a down_streak, unlike with_ha_ban's arm. A Pi deploy recreates containers, so
    # their ports are legitimately closed for a few seconds and a single cycle can read dead.
    # A detached container persists until someone recreates it, so it survives the grace.
    # DECIDED: an attribution fetch that fails downgrades the DIAGNOSIS, never the verdict —
    # pi_ports_verdict renders "cause unknown" and the port is still reported dead. Failing
    # open there would reintroduce exactly the inertness the first DECIDED avoids.
    """
    if not cfg.PI_PUBLISHED_PORTS:
        return ok, msg
    host = urllib.parse.urlsplit(cfg.PI_GLANCES_URL).hostname
    if not host:
        return ok, msg
    dead = [
        (name, port)
        for name, port in cfg.PI_PUBLISHED_PORTS
        if not _tcp_open(host, port, cfg.PI_PORT_TIMEOUT)
    ]
    containers = None
    if dead:
        try:
            containers = _get_json(cfg.PI_GLANCES_URL + "/api/4/containers")
        except Exception:
            containers = None
    arm_ok, arm_msg = pi_ports_verdict(dead, len(cfg.PI_PUBLISHED_PORTS), containers)
    if arm_ok:
        _down_streaks["pi_ports"] = 0
        return ok, "%s, %s" % (msg, arm_msg)
    _down_streaks["pi_ports"], arm_ok, arm_msg = down_streak(
        _down_streaks.get("pi_ports", 0),
        cfg.PI_PORTS_CONSECUTIVE,
        arm_msg,
        "deploy grace",
    )
    if arm_ok:
        return ok, "%s, %s" % (msg, arm_msg)
    return False, "%s | %s" % (arm_msg, msg)


def with_ha_ban(ok, msg):
    """Fold the ip_ban arm into a heartbeat verdict, ban winning the message.

    Folded into this monitor rather than given its own for the reason recorded at
    check_k8s_workloads' extended-resource arm: a new Kuma monitor costs a new push token in
    SOPS, and a ban is an HA fault, which is what this monitor already reports.

    # DECIDED: fails OPEN on a Loki error instead of adding ha_heartbeat to LOKI_DEPENDENT.
    # Membership there suppresses the WHOLE check during a Loki outage, which would blind the
    # real heartbeat — trading a live wedge-detector for a secondary arm is the wrong way round.
    # The ban arm also skips down_streak: down_streak exists to ride out a transient, and a ban is
    # a discrete event that either happened in the window or did not — a second cycle's confirmation
    # would add nothing. Note this arm reports the ban EVENT, not the ban STATE: it self-clears
    # HA_BAN_WINDOW after the ban is issued even though the entry survives in
    # /config/ip_bans.yaml. See the HA_BAN_WINDOW comment for why that is the only signal available.
    """
    try:
        banned = loki_count(cfg.HA_BAN_SELECTOR, cfg.HA_BAN_WINDOW)
    except Exception as e:
        return ok, "%s, ip_ban arm unavailable (%s)" % (msg, e)
    ban_ok, ban_msg = ha_ban_verdict(banned, cfg.HA_BAN_WINDOW)
    if ban_ok:
        return ok, "%s, %s" % (msg, ban_msg)
    return False, "%s | %s" % (ban_msg, msg)


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
    if not cfg.HA_URL or not cfg.HA_TOKEN:
        return True, "HA heartbeat monitoring disabled (no URL/token)"
    try:
        state = _get_json(
            cfg.HA_URL + "/api/states/" + cfg.HA_HEARTBEAT_ENTITY,
            headers={"Authorization": "Bearer " + cfg.HA_TOKEN},
        )
        ok, msg = ha_heartbeat_fresh(state, cfg.HA_HEARTBEAT_MAX_AGE_S)
    except (
        Exception
    ) as e:  # unreachable/auth -> route through the streak, don't page yet
        ok, msg = False, "HA API unreachable: %s" % e
    if ok:
        _down_streaks["ha"] = 0
        return with_ha_ban(True, msg)
    _down_streaks["ha"], ok, msg = down_streak(
        _down_streaks.get("ha", 0), cfg.HA_CONSECUTIVE, msg, "deploy/restart grace"
    )
    return with_ha_ban(ok, msg)


def speedtest_verdict(row, min_mbps, max_age_h, now=None):
    """Pure: judge the newest speedtest-tracker result row. (ok, msg).

    `row` is one element of /api/v1/results' `data`, or None when the app returned no rows at
    all.

    THE TIMESTAMP IS UTC DESPITE CARRYING NO OFFSET. /api/v1/results serializes `created_at` as
    a bare "2026-08-24 11:00:00", while /api/speedtest/latest serializes the SAME row as
    "2026-08-24T06:00:00.000000-05:00" — verified against row id 780 on 2026-08-24. The bare
    form is therefore UTC, not the DISPLAY_TIMEZONE local time it resembles, and
    datetime.fromisoformat returns it naive. Attaching UTC explicitly is what keeps the age
    arm from reading five hours off; a naive value compared against an aware `now` raises
    instead, which is the safer of the two failures but still not a verdict.

    Arms run status, then age, then floor, in that order and for that reason: `download_bits`
    is null on a failed row, so a floor comparison ahead of the status arm compares None.
    """
    now = now or datetime.now(timezone.utc)
    if not row:
        return (
            False,
            "speedtest has no results at all — the scheduler has never completed a run",
        )

    status = row.get("status")
    created = row.get("created_at")

    if status != "completed":
        detail = ((row.get("data") or {}).get("message") or "").strip()
        return False, "last run (%s) %s%s" % (
            created or "unknown time",
            status or "has no status",
            " — " + detail if detail else "",
        )

    if not created:
        return False, "last run has no created_at — cannot judge freshness"
    stamp = datetime.fromisoformat(created.strip().replace(" ", "T"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_h = (now - stamp).total_seconds() / 3600
    if age_h > max_age_h:
        return (
            False,
            "last run was %.1fh ago (> %gh) — the 6-hourly schedule has stopped"
            % (
                age_h,
                max_age_h,
            ),
        )

    bits = row.get("download_bits")
    if bits is None:
        return False, "last run completed but recorded no download figure"
    mbps = float(bits) / 1e6
    server = ((row.get("data") or {}).get("server") or {}).get(
        "name"
    ) or "unknown server"
    if mbps < min_mbps:
        return False, "download %.1f Mbps (< %g) via %s — %.1fh ago" % (
            mbps,
            min_mbps,
            server,
            age_h,
        )
    return True, "download %.1f Mbps via %s, %.1fh ago" % (mbps, server, age_h)


def check_speedtest():
    """Judge speedtest-tracker's newest result row (see the SPEEDTEST_* env block above).

    Empty URL/token -> disabled (stays up), like check_ha_heartbeat.

    NO HYSTERESIS ON THE VERDICT, deliberately. The app runs every 6h and this loop every 5
    min, so a consecutive-cycle streak would re-read the IDENTICAL row up to 72 times: it would
    delay the page by N*INTERVAL and prove nothing new about the run. The FETCH failure does
    ride the streak, because the app restarting under a deploy is a genuine transient — the
    same split check_ha_heartbeat draws, for the same reason. `speedtest` is also in
    STARTUP_GRACE, which covers the post-reboot cycle where the app has not finished booting.
    """
    if not cfg.SPEEDTEST_URL or not cfg.SPEEDTEST_TOKEN:
        return True, "speedtest monitoring disabled (no URL/token)"
    try:
        # sort=-created_at, because the default order is ASCENDING and would hand back the
        # OLDEST row in the 30-day window — a stale-forever reading that looks like a verdict.
        payload = _get_json(
            cfg.SPEEDTEST_URL + "/api/v1/results?sort=-created_at&page%5Bsize%5D=1",
            headers={
                "Authorization": "Bearer " + cfg.SPEEDTEST_TOKEN,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        _down_streaks["speedtest"], ok, msg = down_streak(
            _down_streaks.get("speedtest", 0),
            cfg.SPEEDTEST_CONSECUTIVE,
            "speedtest API unreachable: %s" % e,
            "deploy/restart grace",
        )
        return ok, msg
    _down_streaks["speedtest"] = 0
    rows = payload.get("data") or []
    return speedtest_verdict(
        rows[0] if rows else None,
        cfg.SPEEDTEST_DOWNLOAD_MIN_MBPS,
        cfg.SPEEDTEST_MAX_AGE_H,
    )


def loki_count(selector, window):
    """Instant LogQL query: total log lines for `selector` over `window`. None if no series.

    Loki's instant-query endpoint evaluates a metric query — here
    sum(count_over_time(SELECTOR[WINDOW])) — and returns a vector with the same
    [ts, value] shape prom_scalar parses, so we read result[0].value[1].
    """
    query = "sum(count_over_time(%s[%s]))" % (selector, window)
    result = _instant_query(cfg.LOKI_URL, "/loki/api/v1/query", query, "loki")
    if not result:
        return None
    return float(result[0]["value"][1])


def loki_vector(query):
    """Instant LogQL query keeping each series' labels — the loki_count peer of prom_vector.

    Not prom_vector(base=LOKI_URL): Loki's instant endpoint is /loki/api/v1/query, and
    prom_vector hardcodes /api/v1/query. Same envelope, different path.
    """
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in _instant_query(cfg.LOKI_URL, "/loki/api/v1/query", query, "loki")
    ]


def log_error_counts(selector, pattern, window, by_label="container"):
    """(matches, total) — per-container counts of `pattern`, and the selector's total volume.

    `total` is what keeps this arm honest. The whole arm fails OPEN (see with_log_errors), so a
    selector that matches no stream returns no matches and reads exactly like a healthy estate
    — the trap that shipped HA_BAN_SELECTOR with an `app` label promtail does not emit, and
    pushed "no ip_ban events" through a window containing a real ban. Counting the selector's
    own volume separates "nothing is wrong" from "I asked the wrong question".
    """
    matches = loki_vector(
        "sum by (%s) (count_over_time(%s |~ `%s` [%s]))"
        % (by_label, selector, pattern, window)
    )
    total = loki_count(selector, window)
    return matches, total


def check_loki_ingestion():
    # Two arms, down if EITHER pipeline is silent: the file-tail union (arm 1) catches a
    # file-tail break (all of authlog/syslog/traefik going silent — a total promtail death or
    # a static_configs/bind regression) over a tolerant window; the container-stream arm
    # (arm 2) catches a docker_sd-specific break the file-tail selector excludes (see
    # LOKI_DOCKER_STREAM). The docker stream dwarfs the file-tail streams, so arm 1 must NOT
    # include it (else a healthy docker stream masks a dead file-tail pipeline) — hence the
    # separate selector + wider window (LOKI_FILETAIL_WINDOW).
    ok_all, msg_all = loki_ingestion_fresh(
        loki_count(cfg.LOKI_STREAM, cfg.LOKI_FILETAIL_WINDOW), cfg.LOKI_FILETAIL_WINDOW
    )
    if not ok_all:
        return False, "file-tail streams silent — " + msg_all
    ok_docker, msg_docker = loki_ingestion_fresh(
        loki_count(cfg.LOKI_DOCKER_STREAM, cfg.LOKI_WINDOW), cfg.LOKI_WINDOW
    )
    if not ok_docker:
        return False, "container log stream silent — " + msg_docker
    # Arm 3: the Pi ships its own logs and neither arm above counts them, so its promtail
    # dying is invisible while the cluster keeps talking.
    ok_pi, msg_pi = loki_ingestion_fresh(
        loki_count(cfg.LOKI_PI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_pi:
        return False, "daniel-pi log stream silent — " + msg_pi
    return True, "%s (+ container stream, + pi)" % msg_all


def check_promtail_dropped():
    """Prometheus-based promtail partial-loss watchdog (see promtail_dropped). Prom-dependent."""
    count = prom_scalar(
        "sum(increase(%s[%s]))"
        % (cfg.PROMTAIL_DROPPED_SELECTOR, cfg.PROMTAIL_DROPPED_WINDOW)
    )
    return promtail_dropped(
        count, cfg.PROMTAIL_DROPPED_WINDOW, cfg.PROMTAIL_DROPPED_MAX
    )


def loki_reachable():
    """Is Loki itself reachable and answering queries? (the LOKI_DEPENDENT gate).

    Hits the labels endpoint — a fixed, ingestion-independent query that returns status=success
    whenever Loki is up — so 'Loki is down' (one root cause, one page: Loki Reachable) is separated
    from 'Loki is up but promtail stopped shipping' (Loki Log Ingestion, which still evaluates
    whenever Loki is reachable). Raising -> _evaluate renders the Loki Reachable monitor down.
    """
    data = _get_json(cfg.LOKI_URL + "/loki/api/v1/labels")
    if data.get("status") != "success":
        raise RuntimeError("loki labels status=%s" % data.get("status"))
    return True


def check_loki_reachable():
    loki_reachable()
    return True, "Loki reachable"


# `ttl` is how long THIS cached verdict is held, chosen per outcome by b2_reachable — a billed
# answer from B2 holds B2_PROBE_INTERVAL_S, a transport failure holds B2_TRANSPORT_RETRY_S.
_b2_probe = {
    "ts": 0.0,
    "ok": True,
    "msg": "not yet probed",
    "ttl": cfg.B2_PROBE_INTERVAL_S,
}
_b2_storage = {"ts": 0.0, "ok": False, "msg": "not yet probed"}


def b2_authorize_data():
    """The parsed b2_authorize_account response. Raises on any transport/HTTP failure."""
    token = base64.b64encode(
        ("%s:%s" % (cfg.B2_PROBE_KEY_ID, cfg.B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    return _get_json(cfg.B2_PROBE_URL, headers={"Authorization": "Basic %s" % token})


def b2_storage_api(auth):
    """(api_url, authorization_token, bucket_id) from an authorize response.

    v3 groups the storage endpoint under `apiInfo.storageApi` where v1/v2 had `apiUrl` at the top
    level, so both shapes are read — the same version-tolerance b2_authorize applies to its own
    fields. `bucketId` is present when the application key is bucket-scoped, which this one is;
    without it there is no bucket to sum and the caller reports that rather than guessing.
    """
    storage = (auth.get("apiInfo") or {}).get("storageApi") or {}
    api_url = storage.get("apiUrl") or auth.get("apiUrl")
    bucket_id = storage.get("bucketId") or (auth.get("allowed") or {}).get("bucketId")
    return api_url, auth.get("authorizationToken"), bucket_id


def b2_sum_versions(pages):
    """(total_bytes, version_count) over an iterable of b2_list_file_versions payloads.

    Sums `contentLength` across ALL versions, including hidden ones and the unfinished large-file
    parts that a plain object listing omits — those bill as stored bytes, and omitting them is the
    specific way this number reads lower than the invoice.
    """
    total = 0
    count = 0
    for page in pages:
        for f in page.get("files") or []:
            size = f.get("contentLength")
            if size is None:
                size = f.get("size")
            total += int(size or 0)
            count += 1
    return total, count


def b2_storage_verdict(used_bytes, versions, truncated, cap=None, max_pct=None):
    """(ok, msg) for B2 storage headroom against the free-tier cap."""
    cap = cfg.B2_STORAGE_CAP_BYTES if cap is None else cap
    max_pct = cfg.B2_STORAGE_MAX_PCT if max_pct is None else max_pct
    if not cap:
        return False, "B2 storage cap not configured"
    pct = 100.0 * used_bytes / cap
    detail = "%.2f GB of %.0f GB (%.0f%%), %d versions" % (
        used_bytes / 1000**3,
        cap / 1000**3,
        pct,
        versions,
    )
    if truncated:
        # Under-reporting is the dangerous direction, so a truncated walk is a failure, not a
        # smaller number reported confidently.
        return (
            False,
            "B2 storage listing truncated at %d pages — %s is a FLOOR, not the total"
            % (
                cfg.B2_STORAGE_MAX_PAGES,
                detail,
            ),
        )
    if pct > max_pct:
        return False, "B2 storage over %.0f%%: %s" % (max_pct, detail)
    return True, "B2 storage %s" % detail


def b2_storage_usage(now=None):
    """Throttled B2 storage-headroom probe. (ok, msg).

    SUCCESSES are cached for B2_STORAGE_INTERVAL_S and a failure is not, the
    EMAIL_PROBE_INTERVAL_S idiom rather than b2_reachable's cache-both: a listing failure is far
    more likely to be a transient 5xx than a cap, and b2_reachable already owns the cap signal, so
    there is no spend spiral to protect against here. Empty credentials -> disabled (stays up).
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 storage check disabled (no credentials)"
    now = now if now is not None else time.time()
    if _b2_storage["ok"] and now - _b2_storage["ts"] < cfg.B2_STORAGE_INTERVAL_S:
        return _b2_storage["ok"], "%s (checked %.0fh ago)" % (
            _b2_storage["msg"],
            (now - _b2_storage["ts"]) / 3600,
        )
    try:
        api_url, token, bucket_id = b2_storage_api(b2_authorize_data())
        if not api_url or not token:
            raise RuntimeError("B2 auth response carried no storage apiUrl/token")
        if not bucket_id:
            raise RuntimeError(
                "B2 key is not bucket-scoped (no bucketId) — cannot size a bucket"
            )
        pages, truncated = b2_list_versions(api_url, token, bucket_id)
        used, versions = b2_sum_versions(pages)
        ok, msg = b2_storage_verdict(used, versions, truncated)
    except Exception as e:
        ok, msg = False, "B2 storage probe failed: %s" % e
    _b2_storage["ts"] = now
    _b2_storage["ok"] = ok
    _b2_storage["msg"] = msg
    return ok, msg


def b2_list_versions(api_url, token, bucket_id):
    """(pages, truncated) — every b2_list_file_versions page for the bucket.

    Paginates on the (nextFileName, nextFileId) cursor B2 returns; a page with neither is the
    last. Stops at B2_STORAGE_MAX_PAGES and says so, rather than looping on a cursor that never
    clears.
    """
    pages = []
    start_name = start_id = None
    for _ in range(cfg.B2_STORAGE_MAX_PAGES):
        payload = {"bucketId": bucket_id, "maxFileCount": 1000}
        if start_name:
            payload["startFileName"] = start_name
        if start_id:
            payload["startFileId"] = start_id
        page = _post_json(
            "%s/b2api/v3/b2_list_file_versions" % api_url.rstrip("/"),
            payload,
            headers={"Authorization": token},
        )
        pages.append(page)
        start_name = page.get("nextFileName")
        start_id = page.get("nextFileId")
        if not start_name and not start_id:
            return pages, False
    return pages, True


def check_b2_storage():
    return b2_storage_usage()


def b2_authorize():
    """Authenticate against B2. (ok, msg) — the msg carries B2's own error text on failure.

    Basic auth with the key id + application key is the whole protocol for b2_authorize_account.
    _get_json re-raises HTTPError with the response body appended, so a cap breach arrives here as
    "HTTP Error 403: ... transaction_cap_exceeded ..." and that string is what reaches Kuma and
    Discord — the named cause G3 asked for.
    """
    token = base64.b64encode(
        ("%s:%s" % (cfg.B2_PROBE_KEY_ID, cfg.B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    data = _get_json(cfg.B2_PROBE_URL, headers={"Authorization": "Basic %s" % token})
    # A 200 from something that isn't B2 must not read as healthy. Accept EITHER field rather than
    # pinning the response shape: Backblaze publishes a body example for v4 (accountId top-level)
    # but not for v3, whose documented change was to group endpoint info under `apiInfo`. Both
    # fields have been present since v1, so this survives a version bump either way — and a wrong
    # guess here would page every cycle rather than fail safe.
    if not (data.get("accountId") or data.get("authorizationToken")):
        return False, "B2 auth returned neither accountId nor authorizationToken"
    return True, "B2 reachable"


def b2_reachable(now=None):
    """Throttled B2 reachability probe — the gate for the B2_DEPENDENT checks. (ok, msg).

    Empty credentials -> disabled (stays up), like check_n8n's empty API key. Outcomes are cached
    rather than re-probed every cycle: unlike email_backstop, the failure being detected is a
    transaction cap, and retrying would spend more of the budget this check exists to watch. The
    cached verdict is returned (and pushed) every cycle regardless, so the push monitor's heartbeat
    stays alive and the dead-bridge watchdog isn't tripped.

    The cache TTL depends on WHERE the probe failed, because only one of the two shapes costs a B2
    transaction:

      * A response from B2 — success, or an HTTPError such as the 403 carrying
        `transaction_cap_exceeded` — reached the API and was billed. Cached for
        B2_PROBE_INTERVAL_S (30 min), so a cap breach is not re-spent every cycle.
      * Anything else (DNS, connect, timeout) never reached B2 and was billed nothing.
        _get_json wraps exactly this class as RuntimeError while re-raising HTTPError untouched,
        which is what makes the two separable here. Cached for B2_TRANSPORT_RETRY_S (one cycle).

    Without that split, one transient failure pinned the gate DOWN for the full 30 minutes: on the
    2026-08-30 restart the bridge's first cycle probed B2 before cluster egress was serving, and
    `B2 Reachable` then read DOWN for 25 minutes against an 8m35s outage — the cache was holding
    back the RECOVERY, not just the retry. Re-probing a connection that never landed is free, so
    there is nothing to protect there.

    Module-global cache, reset on container restart, like the streak counters.
    """
    if not cfg.B2_PROBE_KEY_ID or not cfg.B2_PROBE_APPLICATION_KEY:
        return True, "B2 reachability check disabled (no credentials)"
    now = now if now is not None else time.time()
    if now - _b2_probe["ts"] < _b2_probe["ttl"]:
        return _b2_probe["ok"], "%s (checked %.0fm ago)" % (
            _b2_probe["msg"],
            (now - _b2_probe["ts"]) / 60,
        )
    try:
        ok, msg = b2_authorize()
        ttl = cfg.B2_PROBE_INTERVAL_S
    except urllib.error.HTTPError as e:
        # B2 answered, so the call was billed — hold the full interval.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_PROBE_INTERVAL_S
    except Exception as e:
        # Never reached B2, so nothing was billed — retry on the next cycle.
        ok, msg, ttl = False, "B2 unreachable: %s" % e, cfg.B2_TRANSPORT_RETRY_S
    _b2_probe["ts"] = now
    _b2_probe["ok"] = ok
    _b2_probe["msg"] = msg
    _b2_probe["ttl"] = ttl
    return ok, msg


def check_b2_reachable():
    return b2_reachable()


def r2_month_start(now):
    """UTC midnight on the 1st of the calendar month containing `now` (epoch seconds).

    R2's free tier resets on the calendar month, so month-to-date is the only window whose
    percentages mean anything — a rolling 30d window would report headroom that does not exist
    on the 2nd and headroom that has already been given back on the 30th.
    """
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def r2_classify_operations(rows):
    """(class_a, class_b, unknown_actions) from r2OperationsAdaptiveGroups rows.

    An actionType in neither published class list counts toward CLASS A — the expensive one — and
    is named in the verdict. Cloudflare adds operations over time, and the alternative readings are
    both worse: counting an unknown as Class B under-reports the arm with a 10x tighter limit, and
    dropping it makes new operations invisible. Over-counting reports headroom we do not have,
    which is the direction a guard should err in, and the named action says why the numbers moved.
    """
    class_a = class_b = 0
    unknown = {}
    for row in rows:
        action = (row.get("dimensions") or {}).get("actionType") or "unknown"
        requests = (row.get("sum") or {}).get("requests") or 0
        if action in cfg.R2_CLASS_B_ACTIONS:
            class_b += requests
        elif action in cfg.R2_FREE_ACTIONS:
            continue
        elif action in cfg.R2_CLASS_A_ACTIONS:
            class_a += requests
        else:
            class_a += requests
            unknown[action] = unknown.get(action, 0) + requests
    return class_a, class_b, sorted(unknown)


def _pct(used, limit):
    """Percent of `limit` used, or None when the limit is disabled (<= 0)."""
    if limit <= 0:
        return None
    return 100.0 * used / limit


def r2_usage_verdict(
    storage_bytes,
    uploads,
    class_a,
    class_b,
    unknown_actions,
    storage_max_gb=None,
    class_a_max=None,
    class_b_max=None,
    uploads_max=None,
    max_pct=None,
):
    """(ok, msg) from month-to-date R2 usage against the free-tier limits.

    Reports all three arms every cycle whether or not any breaches, so the Kuma message carries
    the trend and not just the alarm — the point of the monitor is to see a runaway client early.
    """
    storage_max_gb = cfg.R2_STORAGE_MAX_GB if storage_max_gb is None else storage_max_gb
    class_a_max = cfg.R2_CLASS_A_MAX if class_a_max is None else class_a_max
    class_b_max = cfg.R2_CLASS_B_MAX if class_b_max is None else class_b_max
    uploads_max = cfg.R2_UPLOADS_MAX if uploads_max is None else uploads_max
    max_pct = cfg.R2_USAGE_MAX_PCT if max_pct is None else max_pct

    storage_gb = storage_bytes / 1e9  # R2 bills decimal GB, not GiB
    arms = (
        ("storage", storage_gb, storage_max_gb, "%.2f/%.0f GB"),
        ("Class A", class_a, class_a_max, "%.0f/%.0f"),
        ("Class B", class_b, class_b_max, "%.0f/%.0f"),
    )
    parts = []
    breaching = []
    for label, used, limit, fmt in arms:
        pct = _pct(used, limit)
        if pct is None:
            parts.append("%s %s (no limit set)" % (label, fmt % (used, limit)))
            continue
        parts.append("%s %s (%.0f%%)" % (label, fmt % (used, limit), pct))
        if pct >= max_pct:
            breaching.append("%s at %.0f%%" % (label, pct))

    if uploads_max > 0 and uploads > uploads_max:
        breaching.append(
            "%d incomplete multipart uploads (they bill as storage and do not show in a "
            "listing — check the bucket's AbortIncompleteMultipartUpload lifecycle rule)"
            % uploads
        )

    msg = "R2 month-to-date: " + ", ".join(parts)
    if unknown_actions:
        msg += " [unclassified ops counted as Class A: %s]" % ", ".join(unknown_actions)
    if breaching:
        return False, "over %.0f%% of free tier — %s. %s" % (
            max_pct,
            "; ".join(breaching),
            msg,
        )
    return True, msg


R2_QUERY = """query {
  viewer {
    accounts(filter: {accountTag: %(account)s}) {
      storage: r2StorageAdaptiveGroups(
        limit: 1
        filter: {bucketName: %(bucket)s, datetime_geq: %(storage_since)s}
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize uploadCount }
        dimensions { datetime }
      }
      operations: r2OperationsAdaptiveGroups(
        limit: 100
        filter: {bucketName: %(bucket)s, datetime_geq: %(month_start)s}
      ) {
        dimensions { actionType }
        sum { requests }
      }
    }
  }
}"""


def r2_query_usage(now):
    """(storage_bytes, uploads, class_a, class_b, unknown_actions) for the current month.

    One POST for both datasets — same account scope, so splitting it would double the calls and
    the error paths for nothing.
    """
    month_start = r2_month_start(now)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    query = R2_QUERY % {
        "account": json.dumps(cfg.CF_ACCOUNT_ID),
        "bucket": json.dumps(cfg.R2_BUCKET),
        "month_start": json.dumps(month_start.strftime(fmt)),
        # Storage is a point-in-time series, not a sum: one row per datetime bucket, so take the
        # most recent and look back far enough that a quiet bucket still has one. `datetime` is
        # selected as a dimension because the orderBy key has to be one — Cloudflare's own storage
        # example does exactly this, and dropping it risks a rejected query, which this check
        # would then report as `down` every cycle.
        "storage_since": json.dumps(
            (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=2)).strftime(
                fmt
            )
        ),
    }
    data = _post_json(
        cfg.CF_GRAPHQL_URL,
        {"query": query},
        headers={"Authorization": "Bearer %s" % cfg.CF_ANALYTICS_TOKEN},
    )
    # Cloudflare answers 200 with a populated `errors` on a bad query or an under-scoped token, so
    # this is the only place a wrong token surfaces. Left unchecked it would read as a zero-usage
    # bucket — a monitor green because it is blind, the failure this file keeps re-learning.
    errors = data.get("errors")
    if errors:
        raise RuntimeError(
            "Cloudflare GraphQL: %s"
            % "; ".join(str(e.get("message", e)) for e in errors)[:FETCH_BODY_MAX]
        )
    accounts = ((data.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise RuntimeError(
            "Cloudflare GraphQL returned no account for accountTag — wrong CF_ACCOUNT_ID, "
            "or the token is not scoped to this account"
        )
    account = accounts[0]
    storage_rows = account.get("storage") or []
    if storage_rows:
        peak = storage_rows[0].get("max") or {}
        storage_bytes = (peak.get("payloadSize") or 0) + (peak.get("metadataSize") or 0)
        uploads = peak.get("uploadCount") or 0
    else:
        # An empty bucket genuinely reports no storage rows; that is 0 bytes, not a fault.
        storage_bytes = uploads = 0
    class_a, class_b, unknown = r2_classify_operations(account.get("operations") or [])
    return storage_bytes, uploads, class_a, class_b, unknown


# ts=None means never probed. An explicit sentinel rather than 0.0: "0 seconds since the epoch" is
# indistinguishable from a real timestamp by the arithmetic below, and only the sheer size of a
# real time.time() keeps that from reading as a fresh cache entry on the first cycle.
_r2_probe = {"ts": None, "ok": True, "msg": ""}


def r2_usage(now=None):
    """Throttled R2 free-tier headroom check. (ok, msg).

    SUCCESSES are cached for R2_PROBE_INTERVAL_S — month-to-date aggregates do not move on a 300s
    cycle, and Cloudflare's GraphQL API is rate-limited per account. A FAILURE is not cached: these
    calls are free and count against no R2 budget, so unlike b2_reachable there is nothing to
    protect by holding a stale verdict, and a re-probe next cycle detects recovery sooner. The
    one-cycle blip that re-probing would otherwise page on is absorbed by STARTUP_GRACE.
    """
    if not cfg.CF_ACCOUNT_ID or not cfg.CF_ANALYTICS_TOKEN or not cfg.R2_BUCKET:
        return True, "R2 usage check disabled (no account id / token / bucket)"
    now = now if now is not None else time.time()
    if (
        _r2_probe["ts"] is not None
        and _r2_probe["ok"]
        and now - _r2_probe["ts"] < cfg.R2_PROBE_INTERVAL_S
    ):
        return True, "%s (checked %.0fm ago)" % (
            _r2_probe["msg"],
            (now - _r2_probe["ts"]) / 60,
        )
    storage_bytes, uploads, class_a, class_b, unknown = r2_query_usage(now)
    ok, msg = r2_usage_verdict(storage_bytes, uploads, class_a, class_b, unknown)
    _r2_probe["ts"] = now
    _r2_probe["ok"] = ok
    _r2_probe["msg"] = msg
    return ok, msg


def check_r2_usage():
    return r2_usage()


def check_k8s_workloads():
    """Deployment readiness for every workload in the k3s cluster.

    Gated by check_cluster_prometheus rather than the ordinary Prometheus gate: this is the one
    check reading the CLUSTER Prometheus, so the `prom_ok` gate is not watching its source. See
    CLUSTER_DEPENDENT.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "k8s workload check disabled (no CLUSTER_PROMETHEUS_URL)"
    total = prom_scalar(
        "count(kube_deployment_status_replicas_unavailable)",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    offenders = prom_vector(
        "kube_deployment_status_replicas_unavailable > 0",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    # The second clause is the recency gate (K8S_RESTART_RECENT_WINDOW): it keeps a recovered
    # pod from holding the tile red for the rest of the 1h evidence window. `and` is a vector
    # match on the full label set, so it filters the first clause's series rather than
    # replacing them — the offender labels reaching the verdict are unchanged.
    restart_offenders = prom_vector(
        "increase(kube_pod_container_status_restarts_total[%s]) > %d"
        " and increase(kube_pod_container_status_restarts_total[%s]) > 0"
        % (cfg.K8S_RESTART_WINDOW, cfg.K8S_RESTART_MAX, cfg.K8S_RESTART_RECENT_WINDOW),
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_total = prom_scalar(
        "count(kube_daemonset_status_number_unavailable)",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_offenders = prom_vector(
        "kube_daemonset_status_number_unavailable > 0",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ok, msg = k8s_workloads_verdict(
        total,
        offenders,
        cfg.K8S_MIN_WORKLOADS,
        restart_offenders,
        ds_total,
        ds_offenders,
        cfg.K8S_MIN_DAEMONSETS,
    )
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and this arm answers the same question the DaemonSet arm does — is the
    # cluster still able to run the workloads that depend on it.
    advertised = {}
    for resource in cfg.K8S_EXTENDED_RESOURCES:
        advertised[resource] = len(
            prom_vector(
                'kube_node_status_allocatable{resource="%s"} > 0'
                % ksm_resource_label(resource),
                base=cfg.CLUSTER_PROM_URL,
                source="cluster prometheus",
            )
        )
    res_ok, res_msg = extended_resource_verdict(
        cfg.K8S_EXTENDED_RESOURCES,
        advertised,
        prom_scalar(
            "count(kube_node_status_allocatable)",
            base=cfg.CLUSTER_PROM_URL,
            source="cluster prometheus",
        ),
    )
    if not res_ok:
        # The resource fault wins the message: an unschedulable-by-design cluster is more urgent
        # than whatever the workload arm has to say, and the workload arm's own text is preserved
        # after it rather than dropped.
        return with_log_errors(False, "%s | %s" % (res_msg, msg))
    return with_log_errors(ok, "%s, %s" % (msg, res_msg))


def with_log_errors(ok, msg):
    """Fold the log-pattern arm into the workload verdict, a burst winning the message.

    Folded here rather than given its own monitor, for the reason the extended-resource and
    ip_ban arms were: a new Kuma monitor needs a new push token in SOPS, and this arm answers
    the question the other arms leave open. They read Kubernetes state — replicas, restarts,
    allocatable — and every one of them reports a container that is Ready while failing at its
    job as healthy, because by their measure it is.

    FAILS OPEN on a Loki error, and is deliberately NOT in LOKI_DEPENDENT: membership there
    suppresses the WHOLE check during a Loki outage, which would blind the three Kubernetes
    arms that have nothing to do with Loki. Same reasoning as ha_heartbeat's ban arm.
    """
    if not cfg.LOG_ERROR_SELECTOR:
        return ok, msg
    ignore = {n.strip().lower() for n in cfg.LOG_ERROR_IGNORE.split(",") if n.strip()}
    try:
        matches, total = log_error_counts(
            cfg.LOG_ERROR_SELECTOR, cfg.LOG_ERROR_PATTERN, cfg.LOG_ERROR_WINDOW
        )
    except Exception as e:
        return ok, "%s, log-error arm unavailable (%s)" % (msg, e)
    log_ok, log_msg = log_error_verdict(
        matches, total, cfg.LOG_ERROR_MAX, cfg.LOG_ERROR_WINDOW, ignore
    )
    if log_ok:
        return ok, "%s, %s" % (msg, log_msg)
    return False, "%s | %s" % (log_msg, msg)


def check_cluster_targets():
    """Scrape targets of the CLUSTER's own Prometheus (the other half of Scrape Targets).

    B5 pinned check_targets_down to origin="daniel-server" so it kept meaning exactly what it
    always meant. The cost of that, unpaid until now, is that the cluster's own five targets were
    watched by nothing: cluster_prometheus probes only reachability, and k8s_workloads reads
    deployment replicas rather than scrape health. kube-state-metrics failing is covered by
    accident (its series vanish and the workload check fails closed on the floor), but
    otel-collector and otel-collector-internal going down was silent — and those two carry the
    only copy of Claude Code's session/token/cost telemetry.

    `origin!="daniel-server"` is everything the pinned sibling does NOT cover: cluster-native
    series, whose `origin` label is absent (PromQL treats an absent label as empty, so `!=` on a
    non-empty value matches them), plus daniel-box's own. The earlier `origin=""` caught only the
    first, which left `up{job="node",origin="daniel-box"}` matching NEITHER check — daniel-box's
    node-exporter could die watched by nothing. Complementary selectors, so every `up` series in
    this Prometheus belongs to exactly one of the two checks. The same floor logic as its sibling,
    so an emptied `up` reads as UNKNOWN rather than as nothing being wrong.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "cluster target check disabled (no CLUSTER_PROMETHEUS_URL)"
    vec = prom_vector(
        'up{origin!="daniel-server"}',
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    return targets_verdict(vec, cfg.CLUSTER_TARGETS_MIN)


def check_cluster_prometheus():
    """Reachability gate for the cluster Prometheus — the peer of check_prometheus.

    Kept separate from the Docker Prometheus gate on purpose. They are different instances on
    different hosts reached by different paths, so one `prom_ok` cannot describe both: a gate
    that is not watching a check's actual source is worse than no gate, because it reports
    confidence it does not have.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "cluster Prometheus check disabled (no CLUSTER_PROMETHEUS_URL)"
    value = prom_scalar(
        "vector(1)", base=cfg.CLUSTER_PROM_URL, source="cluster prometheus"
    )
    if value is None:
        return False, "cluster Prometheus returned no result for vector(1)"
    return True, "cluster Prometheus reachable"


def _discord_webhooks():
    """(label, url) pairs for each configured Discord webhook to verify (skips empties).

    Kuma's is the alert-chain delivery hop for every monitor; CrowdSec's is the independent
    security-ban delivery hop with no other backstop; GitOps/Renovate's carries the gitops-deploy
    rollback alert AND the renovate_notify digests (whose "alive" marker greens regardless of
    delivery); Arr's carries the *arr apps' own onHealthIssue alerts (direct POST from their
    in-app Discord Connect, config only in the app DBs); Healthchecks' is the healthchecks.io app's
    own check-down/up webhook (config only in hc.sqlite, a redundant secondary to its SMTP path).
    None has a Kuma backstop, so all five are verified together.
    """
    return [
        (label, url)
        for label, url in (
            ("Kuma", cfg.DISCORD_WEBHOOK_URL),
            ("CrowdSec", cfg.DISCORD_CROWDSEC_WEBHOOK_URL),
            ("GitOps/Renovate", cfg.DISCORD_GITOPS_WEBHOOK_URL),
            ("Arr", cfg.DISCORD_ARR_WEBHOOK_URL),
            ("Healthchecks", cfg.DISCORD_HEALTHCHECKS_WEBHOOK_URL),
        )
        if url
    ]


def _smtp_login_ok():
    """Connect to the SMTP server over implicit TLS and AUTH with the notify creds. (ok, msg).

    A revoked/expired Gmail app-password fails at login; a broken SMTP endpoint fails at connect. NOOP
    then QUIT — never sends a message. Raises are caught by the caller and ridden through the streak.
    """
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=HTTP_TIMEOUT, context=ctx
    ) as s:
        s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
        s.noop()
    return True, "SMTP login ok (%s)" % cfg.SMTP_USER


_email_probe = {"ts": 0.0, "ok": True, "msg": "not yet probed"}


def email_backstop(now=None):
    """Throttled deliverability probe for the alert-email 2nd channel. (ok, msg).

    Empty SMTP_PASSWORD -> disabled (stays up). A SUCCESS is cached for EMAIL_PROBE_INTERVAL_S (so
    Gmail doesn't see an AUTH every cycle); a FAILURE isn't cached, so it re-probes every cycle until
    it recovers — and check_discord's DISCORD_CONSECUTIVE streak rides out a transient blip before
    paging. Module-global cache, reset on container restart, like the streak counters — no persistent
    state needed.
    """
    if not cfg.SMTP_PASSWORD:
        return True, "email backstop disabled (no SMTP password)"
    now = now if now is not None else time.time()
    if _email_probe["ok"] and now - _email_probe["ts"] < cfg.EMAIL_PROBE_INTERVAL_S:
        return True, "email backstop ok (verified %.1fh ago)" % (
            (now - _email_probe["ts"]) / 3600
        )
    try:
        ok, msg = _smtp_login_ok()
    except (
        Exception
    ) as e:  # revoked password / SMTP unreachable -> ride the check_discord streak
        ok, msg = False, "email backstop SMTP login FAILED: %s" % e
    if ok:
        _email_probe["ts"] = now
    _email_probe["ok"] = ok
    _email_probe["msg"] = msg
    return ok, msg


def check_discord():
    """GET-verify EVERY configured Discord notification webhook still delivers, plus the email backstop.

    Verifies the Kuma alert webhook, the CrowdSec ban-alert webhook, AND the GitOps/Renovate
    webhook (the latter two have no Kuma backstop). `down` if ANY is invalid, naming which. Each
    empty URL is skipped; all empty -> disabled (stays up), like
    check_n8n. Also probes the alert-email 2nd channel (email_backstop) — the independent delivery
    path this same monitor relies on when its Discord webhook is dead — so a silently revoked SMTP
    credential surfaces here too. Streak hysteresis (DISCORD_CONSECUTIVE, like check_ha_heartbeat):
    this check reaches the public internet (webhooks + SMTP), so a single transient non-200 / network
    blip pushes `up` with a streak msg and only the Nth straight failure pages — a genuinely dead
    webhook or SMTP credential stays bad and pages.
    """
    webhooks = _discord_webhooks()
    if not webhooks:
        return True, "Discord webhook check disabled (no URL)"
    ok, msg, valid = True, "", []
    for label, url in webhooks:
        try:
            data = _get_json(url)
            w_ok, w_msg = discord_webhook_ok(200, (data or {}).get("name"))
        except urllib.error.HTTPError as e:
            w_ok, w_msg = discord_webhook_ok(e.code)
        except (
            Exception
        ) as e:  # network/DNS blip -> ride the streak, don't page on one cycle
            w_ok, w_msg = False, "unreachable: %s" % e
        if not w_ok:
            ok, msg = False, "%s webhook: %s" % (label, w_msg)
            break
        valid.append(label)
    if ok:
        e_ok, e_msg = email_backstop()
        if e_ok:
            valid.append("email")
        else:
            ok, msg = False, e_msg
    if ok:
        _down_streaks["discord"] = 0
        return True, "delivery channels valid (%s)" % ", ".join(valid)
    _down_streaks["discord"], ok, msg = down_streak(
        _down_streaks.get("discord", 0), cfg.DISCORD_CONSECUTIVE, msg, "transient grace"
    )
    return ok, msg


def check_longhorn_volumes():
    """Longhorn volumes that have lost replica redundancy, named by PVC.

    `k3s_longhorn_replica_count` is 2, so a volume reading `degraded` is down to a single copy —
    still serving, one more failure from data loss — and `faulted` means no healthy replica is
    left at all. Nothing else in CHECKS covers this: k8s_workloads watches the pod, not the
    volume under it, and the backup-plane cron watches Backup objects rather than live replica
    state. Until this arm landed (2026-08-17) replica loss was silent.

    `longhorn_volume_robustness` is ONE-HOT over a `state` label (healthy/degraded/faulted/
    unknown) with value 0 or 1 — four series per volume. So this selects on the label and never
    compares the value to a state ordinal, which is the mistake an earlier proposal made.
    `unknown` means detached and is deliberately not a fault: 6 of 43 volumes read it here,
    including the intentionally scaled-to-zero game servers.

    The two longhorn-manager pods report DISJOINT volume subsets (43 volumes total across both,
    not 43 each), so offenders are deduped by name rather than counted — a raw count would
    change meaning the moment a volume moved between managers.

    An absent metric is treated as a breach, NOT as green. If the longhorn scrape job dies the
    degraded selector returns empty for exactly the same reason a healthy cluster does, and a
    check that cannot distinguish "nothing is wrong" from "I cannot see" is the failure mode
    this estate keeps rediscovering (manifest-prune's unreadable staged dirs, the backup
    reaper's unpopulated owner map). The volume count doubles as that input assertion: the
    one-hot shape guarantees a `state="healthy"` series per volume even when its value is 0.
    """
    volumes = prom_scalar('count(longhorn_volume_robustness{state="healthy"})')
    if not volumes:
        _down_streaks["longhorn"], ok, msg = down_streak(
            _down_streaks.get("longhorn", 0),
            cfg.LONGHORN_CONSECUTIVE,
            "no longhorn_volume_robustness series — replica redundancy is UNMONITORED "
            "(job=longhorn scrape down?), which is not the same as healthy",
            "scrape gap grace",
        )
        return ok, msg
    worst = {}
    for labels, _value in prom_vector(
        'longhorn_volume_robustness{state=~"degraded|faulted"} == 1'
    ):
        name = labels.get("pvc") or labels.get("volume", "?")
        state = labels.get("state", "?")
        # faulted outranks degraded if both ever report for one volume
        if worst.get(name) != "faulted":
            worst[name] = state
    if not worst:
        _down_streaks["longhorn"] = 0
        return True, "%d volume(s) redundant, none degraded or faulted" % int(volumes)
    faulted = sorted(n for n, s in worst.items() if s == "faulted")
    degraded = sorted(n for n, s in worst.items() if s != "faulted")
    parts = []
    if faulted:
        parts.append("%d faulted (%s)" % (len(faulted), ", ".join(faulted[:5])))
    if degraded:
        parts.append(
            "%d degraded, single-copy (%s)" % (len(degraded), ", ".join(degraded[:5]))
        )
    _down_streaks["longhorn"], ok, msg = down_streak(
        _down_streaks.get("longhorn", 0),
        cfg.LONGHORN_CONSECUTIVE,
        "of %d volume(s): %s" % (int(volumes), "; ".join(parts)),
        "drain/reboot grace",
    )
    return ok, msg


def check_pvc_fullness():
    """Filesystem fullness of every PersistentVolumeClaim the kubelet reports stats for.

    Nothing else covered this. check_disk iterates DISK_MOUNTPOINTS — `/`, `/boot`, `/boot/efi`
    — which are host filesystems, and check_longhorn_volumes reads longhorn_volume_robustness,
    which is replica redundancy rather than space. A Longhorn PVC has its own filesystem at a
    fixed capacity, so a 2 Gi claim can reach 100% while both hosts report hundreds of GB free:
    the app starts failing writes and every existing monitor stays green.

    Reads the CLUSTER Prometheus like check_k8s_workloads, so it belongs to CLUSTER_DEPENDENT
    rather than PROM_DEPENDENT — the gate has to be the one watching this check's own source.

    `max by (namespace, persistentvolumeclaim)` — not `sum` or `avg` — because daniel-box's
    claims are scraped TWICE. k3s serves the kubelet's metric registry on the supervisor's
    /metrics as well, so the same series arrives under job="kubernetes-kubelet" and under
    job="kubernetes-apiserver" (measured 2026-09-01: 43 + 27 = 70 series over 43 claims).
    `max` of two copies of one ratio is that ratio, so the double scrape is harmless; `sum`
    would report a double-scraped claim at twice its real fullness and `avg` would silently
    change meaning the day one job's coverage moved. Grouping is also what makes the count
    below a claim census rather than a scrape-job artifact.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "PVC fullness check disabled (no CLUSTER_PROMETHEUS_URL)"
    claims = prom_scalar(
        "count(count by (namespace, persistentvolumeclaim)"
        " (kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    vec = prom_vector(
        "max by (namespace, persistentvolumeclaim) (100 *"
        " (1 - kubelet_volume_stats_available_bytes"
        " / kubelet_volume_stats_capacity_bytes))",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    watched = [
        (labels.get("persistentvolumeclaim", "?"), labels.get("namespace", "?"), pct)
        for labels, pct in vec
        if labels.get("persistentvolumeclaim") not in cfg.PVC_EXCLUDE
    ]
    if not watched:
        # A DIFFERENT fault from a thin claim census below, and it must not reach the `worst`
        # report: the ratio query returned nothing at all, which looks exactly like "no claim is
        # full" and is not the same fact.
        _down_streaks["pvc_fullness"], ok, msg = down_streak(
            _down_streaks.get("pvc_fullness", 0),
            cfg.PVC_CLAIMS_CONSECUTIVE,
            "no PVC reported a fullness ratio — PVC fullness is UNKNOWN, not OK",
            "kubelet scrape gap grace",
        )
        return ok, msg
    # Fullest first, so a truncated message names the claims closest to failing.
    breaching = [
        "%s/%s %.0f%%" % (ns, pvc, pct)
        for pvc, ns, pct in sorted(watched, key=lambda w: w[2], reverse=True)
        if pct > cfg.PVC_MAX_PCT
    ]
    # The floor is the input assertion, and it is evaluated over ALL claims including the
    # excluded ones: it asserts the metric family is being scraped, which is a different
    # question from which claims this arm judges.
    shortfall = None
    if claims is None or claims < cfg.PVC_MIN_CLAIMS:
        seen = "no" if claims is None else "only %d" % int(claims)
        _down_streaks["pvc_fullness"], short_ok, short_msg = down_streak(
            _down_streaks.get("pvc_fullness", 0),
            cfg.PVC_CLAIMS_CONSECUTIVE,
            "%s kubelet_volume_stats claims visible, below the floor of %d — PVC fullness is "
            "UNKNOWN, not OK" % (seen, cfg.PVC_MIN_CLAIMS),
            "kubelet scrape gap grace",
        )
        shortfall = (short_ok, short_msg)
    else:
        _down_streaks["pvc_fullness"] = 0
    # A claim that IS reporting and IS full outranks a complaint about the ones that are not —
    # same ordering as check_disk, and for the same reason.
    if breaching:
        return False, "PVC over %.0f%%: %s" % (
            cfg.PVC_MAX_PCT,
            ", ".join(breaching[:5]),
        )
    if shortfall is not None:
        return shortfall
    worst = max(watched, key=lambda w: w[2])
    return True, "%d claim(s) under %.0f%%, worst %s/%s %.0f%%" % (
        len(watched),
        cfg.PVC_MAX_PCT,
        worst[1],
        worst[0],
        worst[2],
    )


CHECKS = [
    ("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    ("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    ("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
    # restarts/oom/cpu RETARGETED 2026-08-14 (Phase G): retired with the Docker cadvisor
    # the same morning, re-armed the same evening against the kubernetes-cadvisor job's
    # label shape — grouped by pod (`name` is the runtime hash there). Same pure logic,
    # same thresholds; complements k8s_workloads' crashloop paging with OOM + sustained-
    # throttle depth the retirement dropped.
    ("restarts", _env("KUMA_PUSH_RESTARTS", ""), check_restarts),
    ("oom", _env("KUMA_PUSH_OOM", ""), check_oom),
    ("cpu", _env("KUMA_PUSH_CPU", ""), check_cpu_throttle),
    ("targets", _env("KUMA_PUSH_TARGETS", ""), check_targets_down),
    ("traefik5xx", _env("KUMA_PUSH_TRAEFIK", ""), check_traefik_5xx),
    (
        "traefik_latency",
        _env("KUMA_PUSH_TRAEFIK_LATENCY", ""),
        check_traefik_latency,
    ),
    ("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n),
    ("arr_queue", _env("KUMA_PUSH_ARR_QUEUE", ""), check_arr_queue),
    ("bazarr", _env("KUMA_PUSH_BAZARR", ""), check_bazarr),
    (
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    ("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    # Reads a stamp the drill writes weekly rather than a live source, so it is the same shape
    # as the gitops pair above: a hostPath the pod is pinned to, read fail-closed. Its token was
    # minted 2026-08-28, which is what let it be registered — test_checks_and_env_secret_push
    # _tokens_agree blocks a check whose KUMA_PUSH_* name has no env-secret entry, correctly:
    # such a check pushes to nowhere forever, present in the code and absent from the world.
    (
        "etcd_restore_drill",
        _env("KUMA_PUSH_ETCD_DRILL", ""),
        check_etcd_restore_drill,
    ),
    ("scrutiny", _env("KUMA_PUSH_SCRUTINY", ""), check_scrutiny),
    ("host_temp", _env("KUMA_PUSH_HOST_TEMP", ""), check_host_temp),
    ("ups", _env("KUMA_PUSH_UPS", ""), check_ups),
    ("pi_pressure", _env("KUMA_PUSH_PI", ""), check_pi_pressure),
    ("ha_heartbeat", _env("KUMA_PUSH_HA", ""), check_ha_heartbeat),
    ("speedtest", _env("KUMA_PUSH_SPEEDTEST", ""), check_speedtest),
    ("loki_ingestion", _env("KUMA_PUSH_LOKI", ""), check_loki_ingestion),
    (
        "promtail_dropped",
        _env("KUMA_PUSH_PROMTAIL_DROPPED", ""),
        check_promtail_dropped,
    ),
    ("discord", _env("KUMA_PUSH_DISCORD", ""), check_discord),
    ("r2_usage", _env("KUMA_PUSH_R2_USAGE", ""), check_r2_usage),
    ("b2_storage", _env("KUMA_PUSH_B2_STORAGE", ""), check_b2_storage),
    ("k8s_workloads", _env("KUMA_PUSH_K8S_WORKLOADS", ""), check_k8s_workloads),
    ("cluster_targets", _env("KUMA_PUSH_CLUSTER_TARGETS", ""), check_cluster_targets),
    (
        "longhorn_volumes",
        _env("KUMA_PUSH_LONGHORN_VOLUMES", ""),
        check_longhorn_volumes,
    ),
    ("pvc_fullness", _env("KUMA_PUSH_PVC", ""), check_pvc_fullness),
]

# Checks that query Prometheus. A single Prometheus outage would fail every one of them at once
# — one root cause, a storm of identical pages. run_once probes Prometheus first (check_prometheus
# -> its own monitor) and, when it's unreachable, SUPPRESSES these (pushes `up` with a skip msg so
# their push-monitor heartbeat stays alive and the dead-bridge watchdog isn't tripped) so only the
# Prometheus monitor pages. Keep this in sync with the prom_scalar/prom_vector callers above.
PROM_DEPENDENT = frozenset(
    {
        "disk",
        "cert",
        "memory",
        "restarts",
        "oom",
        "cpu",
        "targets",
        "traefik5xx",
        "ups",  # queries HA's Prometheus-scraped UPS battery sensors
        # Reads node_hwmon_temp_celsius. Its empty-vector branch pages on a blind hwmon
        # collector, so a Prometheus outage must suppress it — same reason as longhorn_volumes.
        "host_temp",
        "promtail_dropped",  # increase(promtail_dropped_entries_total) instant query
        # Reads longhorn_volume_robustness. Its own absent-metric branch pages when the
        # longhorn scrape job dies, so it must be suppressed when PROMETHEUS itself is the
        # cause — otherwise a Prometheus outage pages twice for one root cause.
        "longhorn_volumes",
    }
)

# One level BELOW the Prometheus gate: a single exporter down while Prometheus is UP fails every
# check reading its metrics at once. node-exporter death false-pages Root Disk + Memory (node_* go
# unavailable -> down) on top of the legitimate Scrape Targets page; cadvisor death makes
# restarts/oom/cpu read an empty vector -> silently green. Scrape Targets already names the dead
# `up{job=...}==0`, so run_once suppresses each dead exporter's dependents (pushes `up` with a skip
# msg, heartbeat kept alive) and lets Scrape Targets be the single page — the same
# one-root-cause-one-alert shape as the Prometheus gate, keyed by the Prometheus `job` label.
# Guarded by a test against CHECKS. (`cert`/`traefik5xx` read Traefik's own metrics, not these
# two exporters, so they're not mapped here.)
# host_temp joined 2026-08-29 with HWMON_TEMP_ORIGINS_MIN. Before the floor it had no per-host
# arm, so a single node-exporter death left it green and there was nothing to suppress. Now a
# dead node-exporter drops that host's hwmon series and trips the floor, so without this entry
# one root cause would page twice — Scrape Targets plus a coverage complaint naming the same
# host. Same reason disk and memory are here.
#
# The Pi scrapes under its OWN job — measured 2026-08-29, `count by (job, origin)
# (node_hwmon_temp_celsius)` returns job=node for daniel-server (12) and daniel-box (7) but
# job=node-pi for daniel-pi (2). This map is keyed by the Prometheus job, so a `node` entry alone
# suppresses two of the three hosts and the Pi's exporter death still double-pages. node-pi maps
# ONLY to host_temp: disk and memory exclude the Pi by origin (HOST_METRIC_ORIGIN_EXCLUDE, since
# check_pi_pressure owns them), so they have nothing to suppress there, while the hwmon floor
# counts all three hosts.
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory", "host_temp"}),
    "node-pi": frozenset({"host_temp"}),
}

# Loki-reachability gate — the peer of the Prometheus gate for the Loki-querying checks. A single
# Loki outage makes loki_count raise in ALL of them at once (Loki Log Ingestion + Janitorr Errors)
# -> a 2-monitor storm for one root cause. run_once probes Loki first
# (check_loki_reachable -> its own "Loki Reachable" monitor) and, when it's unreachable, SUPPRESSES
# these (pushes `up` with a skip msg so their push heartbeats stay alive) so only Loki Reachable
# pages. Loki being UP but promtail not shipping is a different signal Loki Log Ingestion still
# surfaces (it evaluates whenever Loki is reachable). Guarded by a test against CHECKS.
LOKI_DEPENDENT = frozenset({"loki_ingestion"})

# B2-reachability gate — the third peer of the Prometheus and Loki gates (see check_b2_reachable /
# b2_reachable in run_once), and the fix for G2/G4 of docs/b2-transaction-cap-monitoring-gaps.md.
# It used to gate five kopia-era checks that read B2 health from state files written by periodic
# crons, so they reported the LAST SUCCESSFUL RUN rather than current health: on 2026-08-02 they
# read green through a nine-and-a-half-hour outage in which B2 refused every request. Those checks
# were removed 2026-08-10 — kopia is retired, backup moved to Longhorn (see
# docs/archive/k3s-migration/backup-consolidation-longhorn.md) — leaving this empty. b2_reachable itself
# stays: Longhorn still needs B2.
#
# b2_storage re-populated it on 2026-08-15. It queries B2 live rather than reading a cron's state
# file, so it does not have the stale-state fault the original five had — but it is gated for the
# other reason a gate exists: a transaction cap fails BOTH it and b2_reachable, and one root cause
# must not light two monitors.
B2_DEPENDENT = frozenset({"b2_storage"})

# Checks that read CLUSTER_PROM_URL rather than PROM_URL. Its own gate, not an arm of
# PROM_DEPENDENT, because a gate that is not watching a check's real source reports confidence it
# does not have.
#
# The two URLs used to name two instances on two hosts reached by two paths, and the Docker
# Prometheus being up said nothing about whether the cluster one was. Since the Docker plane
# retired (2026-08-14) PROMETHEUS_URL and CLUSTER_PROMETHEUS_URL render to the SAME cluster
# Service URL, so today both gates observe one instance and run_once reuses the prometheus gate's
# verdict here rather than probing twice. The split survives anyway, because it is what lets a
# second Prometheus be reintroduced without re-deciding which gate watches which check. So
# membership follows the URL a check reads, not which host happens to answer it.
#
# The division of labour with check_k8s_workloads' own fail-closed logic is deliberate and the two
# halves are not interchangeable. THIS gate covers "the cluster Prometheus is unreachable", which
# is a root cause that would otherwise page as a workload fault. The check's series-count floor
# covers "the cluster Prometheus is reachable but kube-state-metrics is not being scraped" — which
# this gate structurally cannot see, because the Prometheus answering `vector(1)` is perfectly
# healthy. Suppression is right for the first and would be dangerous for the second: it would turn
# a blind monitor green.
# pvc_fullness joins for the same reason and with the same division of labour: this gate covers
# "the cluster Prometheus is unreachable", while the check's own claim-count floor covers "the
# cluster Prometheus is answering but the kubelet volume stats are not being scraped". Its
# fail-closed arm pages on an empty vector, so a Prometheus outage must suppress it or one root
# cause lights two monitors.
#
# It is NOT given an EXPORTER_DEPENDENT entry keyed on job="kubernetes-kubelet", which is the
# nearest-looking wiring and would be wrong. Those claims are scraped under two jobs, so a dead
# kubelet job still leaves the apiserver job answering for 27 of the 43 claims — a PARTIAL
# blindness PVC_MIN_CLAIMS is sized to page on, and a job-keyed suppression would turn that page
# green. Same mistake as the `node`-only entry that suppressed two hosts of three for host_temp,
# not a fix for it.
CLUSTER_DEPENDENT = frozenset({"k8s_workloads", "cluster_targets", "pvc_fullness"})

# Reach-out checks that poll a live app dependency (n8n/sonarr/radarr/prowlarr/scrutiny/the Pi
# glances/the Cloudflare GraphQL API) with NO reachability gate above them and NO per-check
# hysteresis of their own — unlike
# check_ha_heartbeat/check_discord, whose HA_CONSECUTIVE/DISCORD_CONSECUTIVE grace rides out exactly
# this. On the bridge's first cycle after the weekly host reboot those dependencies are still
# starting, so an un-graced check flips its max_retries=0 monitor DOWN on that one transient cycle
# and pages (then recovers next cycle). run_once holds each of these `up` for the first
# GRACE_CYCLES-1 consecutive down cycles; the GRACE_CYCLES'th straight down still pages a
# genuinely-dead dependency. Must be DISJOINT from the run_once skip sets
# (PROM_DEPENDENT/LOKI_DEPENDENT/EXPORTER_DEPENDENT) so a graced check reaches the eval path every
# cycle. Guarded by a test against CHECKS: the "real check name" guard PLUS a completeness guard
# that every un-gated _get_json reach-out check is in here (prowlarr_indexers/scrutiny were added
# 2026-07-14 after they were found missing — the weekly-reboot flap's original set omitted them).
STARTUP_GRACE = frozenset(
    {
        "n8n",
        "arr_queue",
        "bazarr",
        "pi_pressure",
        "prowlarr_indexers",
        "scrutiny",
        "r2_usage",
        "speedtest",
    }
)

_grace_streaks = {}

# Which checks THIS instance runs. The Phase F twin/remnant split ended with the Docker
# uninstall (2026-08-14): the cluster deployment is now the ONLY bridge and runs every
# check (the gitops checks re-pointed at daniel-box's deployer via a hostPath — the pod
# is pinned there; disk_prune retired with the Docker daemon; pi_peers/renovate_alive
# became direct pushers at the host flips). The CHECKS_ONLY/CHECKS_SKIP mechanism stays
# — it is how any future split would be expressed, and the guards below keep it honest.
# CHECKS_ONLY (comma-separated names) enables exactly that set; CHECKS_SKIP drops
# names from whatever is otherwise enabled. The four reachability gates participate under
# the names their monitors push as (prometheus, loki_reachable, b2_reachable,
# cluster_prometheus). A filter that enables a gated check while disabling its gate would
# reintroduce the alert storm the gate exists to prevent, so main() refuses to start on one
# (validate_check_filter) — a crash-looping bridge is loud, a mis-gated one lies quietly.
GATE_DEPENDENTS = {
    "prometheus": PROM_DEPENDENT,
    "loki_reachable": LOKI_DEPENDENT,
    "b2_reachable": B2_DEPENDENT,
    "cluster_prometheus": CLUSTER_DEPENDENT,
}


def _name_set(value):
    return frozenset(n for n in value.replace(" ", "").split(",") if n)


CHECKS_ONLY = _name_set(_env("CHECKS_ONLY", ""))
CHECKS_SKIP = _name_set(_env("CHECKS_SKIP", ""))


def check_enabled(name, only=None, skip=None):
    only = CHECKS_ONLY if only is None else only
    skip = CHECKS_SKIP if skip is None else skip
    if only and name not in only:
        return False
    return name not in skip


def validate_check_filter(only, skip, checks):
    """Pure: return the list of problems with a CHECKS_ONLY/CHECKS_SKIP configuration."""
    known = {name for name, _, _ in checks} | set(GATE_DEPENDENTS)
    problems = ["unknown check name: %s" % n for n in sorted((only | skip) - known)]
    for gate, dependents in sorted(GATE_DEPENDENTS.items()):
        if check_enabled(gate, only, skip):
            continue
        enabled = sorted(d for d in dependents if check_enabled(d, only, skip))
        if enabled:
            problems.append(
                "gate %s is disabled but its dependents are enabled: %s"
                % (gate, ", ".join(enabled))
            )
    return problems


def down_streak(count, threshold, msg, grace_note, held_label="down streak"):
    """Pure consecutive-down hysteresis step shared by every per-check grace (check_ha_heartbeat/
    check_ups/check_discord) and apply_startup_grace. Call on a DOWN result — the caller resets its
    own counter to 0 on `ok`. Increments `count` and returns (new_count, hold_ok, out_msg): while
    under `threshold` it holds `up` with a "<held_label> n/N (<grace_note>): msg" note; the
    `threshold`'th straight down pages with "msg (n cycles)". (check_cpu_throttle keeps its own down
    branch — its page message embeds the throttle thresholds, so it can't use the generic format.)
    """
    count += 1
    if count < threshold:
        return (
            count,
            True,
            "%s %d/%d (%s): %s" % (held_label, count, threshold, grace_note, msg),
        )
    return count, False, "%s (%d cycles)" % (msg, count)


def apply_startup_grace(name, ok, msg, threshold, streaks):
    """Pure: hold a reach-out check `up` through the first `threshold`-1 consecutive down cycles.

    `streaks` is a name->consecutive-down-count dict, mutated in place. An `ok` result resets the
    count; a down result advances the shared `down_streak` hysteresis, so a held cycle reads with the
    same "down streak n/N" / "(n cycles)" wording as the HA/UPS/Discord per-check grace.
    """
    if ok:
        streaks[name] = 0
        return ok, msg
    streaks[name], ok, msg = down_streak(
        streaks.get(name, 0), threshold, msg, "startup/redeploy grace"
    )
    return ok, msg


def down_exporters(up_vector):
    """Pure: which EXPORTER_DEPENDENT jobs report up==0 in a Prometheus `up` vector.

    Fed prom_vector("up") — [(labels, value), ...]. Returns the subset of EXPORTER_DEPENDENT keys
    whose Prometheus job is down, so run_once can suppress their dependents. Unit-tested.
    """
    down_jobs = {m.get("job") for m, v in up_vector if v == 0}
    return {job for job in EXPORTER_DEPENDENT if job in down_jobs}


def push(token, ok, msg):
    if not token:
        bridge_common.log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _get_json("%s/api/push/%s?%s" % (cfg.KUMA_URL, token, qs))
    except Exception as e:  # best-effort heartbeat; never crash the loop
        bridge_common.log("push failed (%s):" % msg, e)


def _evaluate(name, fn):
    """Run one check; convert an unreachable source/metric into a descriptive `down` instead
    of letting it kill the loop. Returns (ok, msg)."""
    try:
        return fn()
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return False, "%s check error: %s" % (name, e)


def _gate(name, fn, push_env):
    """Evaluate one reachability gate: verdict, log line, heartbeat push. Returns (ok, msg).

    A gate differs from an ordinary check only in what its verdict is used for — the CHECKS
    loop in run_once() reads it to suppress that gate's dependents, so a single outage pages
    once instead of storming. A disabled gate returns `True` so the filter suppresses nothing.
    """
    if not check_enabled(name):
        return True, "disabled by check filter"
    ok, msg = _evaluate(name, fn)
    bridge_common.log("OK  " if ok else "DOWN", name, "-", msg)
    push(_env(push_env, ""), ok, msg)
    return ok, msg


def run_once():
    # Prometheus reachability is evaluated FIRST and gates the prom-dependent checks: a single
    # Prometheus outage would otherwise page all of them at once (one root cause, an alert storm).
    # When it's down they're suppressed (pushed `up` with a skip msg, keeping each push monitor's
    # heartbeat alive) so only the Prometheus monitor pages; a real per-metric problem still alerts
    # whenever Prometheus is up.
    prom_ok, prom_msg = _gate("prometheus", check_prometheus, "KUMA_PUSH_PROMETHEUS")

    # Exporter-reachability gate (one level below the Prometheus gate): when Prometheus is up, probe
    # `up` once and suppress each dead exporter's dependents so a node-exporter/cadvisor death is one
    # page (Scrape Targets), not a 3-monitor false-page storm / silent-green split. A failure to
    # DETERMINE exporter health leaves `suppressed` empty (fail toward alerting, never masking).
    suppressed = set()
    if prom_ok and check_enabled("prometheus"):
        try:
            for job in down_exporters(prom_vector("up%s" % origin_sel())):
                suppressed |= EXPORTER_DEPENDENT[job]
        except Exception as e:
            bridge_common.log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (LOKI_DEPENDENT).
    loki_ok, loki_msg = _gate(
        "loki_reachable", check_loki_reachable, "KUMA_PUSH_LOKI_REACHABLE"
    )

    # B2-reachability gate (peer of the two above): B2 caps TRANSACTIONS separately from storage
    # bytes, and the kopia-era state-file checks this used to gate all reported their last
    # successful cron run rather than current B2 health — the 2026-08-02 transaction-cap incident.
    # Those checks are gone (backup moved to Longhorn), but b2_reachable stays: Longhorn still
    # needs B2. The probe is throttled inside b2_reachable (it must not spend the transaction
    # budget it is watching), but the cached verdict is pushed every cycle so this monitor's own
    # heartbeat stays alive.
    b2_ok, b2_msg = _gate("b2_reachable", check_b2_reachable, "KUMA_PUSH_B2_REACHABLE")

    # Cluster-Prometheus gate (peer of the Prometheus gate, for the OTHER instance): the cluster
    # checks read daniel-box's Prometheus over the cluster ingress, a path none of the other gates
    # covers. Without this, a cluster ingress/Traefik outage would page as a workload fault rather
    # than as what it is.
    #
    # Since B5 that is usually the SAME instance the `prometheus` gate just probed — PROMETHEUS_URL
    # and CLUSTER_PROMETHEUS_URL both point at the cluster. Re-probing would spend a second request
    # on an answered question and, worse, light up two Kuma monitors for one fact, which reads as
    # more coverage than exists. So the verdict is reused when the URLs match, and only genuinely
    # separate endpoints get a separate probe and a separate page.
    #
    # DECIDED: this gate does NOT go through _gate() — the reuse branch below sits between the
    # check_enabled() test and the log/push, which is exactly the span _gate() owns. Threading a
    # precomputed verdict through would add a parameter for one caller and hide the reuse.
    cluster_ok, cluster_msg = True, "disabled by check filter"
    if check_enabled("cluster_prometheus"):
        # The same-instance reuse only holds when the prometheus gate actually probed.
        if (
            cfg.CLUSTER_PROM_URL
            and cfg.CLUSTER_PROM_URL == cfg.PROM_URL
            and check_enabled("prometheus")
        ):
            cluster_ok, cluster_msg = (
                prom_ok,
                "same instance as the Prometheus gate (%s)" % prom_msg,
            )
        else:
            cluster_ok, cluster_msg = _evaluate(
                "cluster_prometheus", check_cluster_prometheus
            )
        bridge_common.log(
            "OK  " if cluster_ok else "DOWN", "cluster_prometheus", "-", cluster_msg
        )
        push(_env("KUMA_PUSH_CLUSTER_PROMETHEUS", ""), cluster_ok, cluster_msg)

    for name, token, fn in CHECKS:
        if not check_enabled(name):
            continue
        if not prom_ok and name in PROM_DEPENDENT:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not loki_ok and name in LOKI_DEPENDENT:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not b2_ok and name in B2_DEPENDENT:
            ok, msg = True, "skipped — B2 unreachable (see B2 Reachable monitor)"
            bridge_common.log("SKIP", name, "-", msg)
        elif not cluster_ok and name in CLUSTER_DEPENDENT:
            ok, msg = (
                True,
                "skipped — cluster Prometheus unreachable (see Cluster Prometheus monitor)",
            )
            bridge_common.log("SKIP", name, "-", msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see Scrape Targets)"
            bridge_common.log("SKIP", name, "-", msg)
        else:
            ok, msg = _evaluate(name, fn)
            if name in STARTUP_GRACE:
                ok, msg = apply_startup_grace(
                    name, ok, msg, cfg.GRACE_CYCLES, _grace_streaks
                )
            bridge_common.log("OK  " if ok else "DOWN", name, "-", msg)
        push(token, ok, msg)


def main():
    once = "--once" in sys.argv
    problems = validate_check_filter(CHECKS_ONLY, CHECKS_SKIP, CHECKS)
    if problems:
        for p in problems:
            bridge_common.log("FATAL: bad CHECKS_ONLY/CHECKS_SKIP:", p)
        sys.exit(2)
    enabled = [name for name, _, _ in CHECKS if check_enabled(name)]
    bridge_common.log(
        "monitor-bridge starting (interval=%ss, once=%s, checks=%d/%d)"
        % (cfg.INTERVAL, once, len(enabled), len(CHECKS))
    )
    while True:
        run_once()
        bridge_common.touch_heartbeat(cfg.HEARTBEAT_FILE)
        if once:
            break
        time.sleep(cfg.INTERVAL)


if __name__ == "__main__":
    main()
