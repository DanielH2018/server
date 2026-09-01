#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import sys
import time

# This file is the registry and the run loop; every check body lives in a checks_* module.
# A name the test suite patches is read QUALIFIED from the module that binds it — `cfg.X`
# for every threshold and URL, `bridge_io.push`, `bridge_common.log` — never from-imported.
# A from-import copies the value into this module's globals at import time, so a later
# `monkeypatch.setattr(bridge_config, "X", ...)` would change nothing this file reads and
# the test would pass against the real value. The check_* entries below ARE from-imported,
# because run_once reads them from this module's globals and the gates tests patch them
# HERE, on `check`. Enforced by ansible/tests/test_bridge_patch_boundary.py; the census of
# what is patched where is ansible/tests/test_monitor_bridge_modules.py.
import bridge_common
from bridge_common import _env

import bridge_config as cfg
import bridge_io
import bridge_streaks
from checks_notify import (
    check_discord,
)
from checks_service import (
    check_arr_queue,
    check_bazarr,
    check_etcd_restore_drill,
    check_gitops_alive,
    check_gitops_status,
    check_ha_heartbeat,
    check_n8n,
    check_prowlarr_indexers,
)
from checks_cluster import (
    check_cluster_prometheus,
    check_cluster_targets,
    check_cpu_throttle,
    check_k8s_workloads,
    check_oom,
    check_prometheus,
    check_restarts,
    check_targets_down,
    check_traefik_5xx,
    check_traefik_latency,
)
from checks_host import (
    check_cert,
    check_disk,
    check_host_temp,
    check_mem,
    check_pi_pressure,
    check_scrutiny,
    check_speedtest,
    check_ups,
)
from checks_storage import (
    check_b2_reachable,
    check_b2_storage,
    check_longhorn_volumes,
    check_pvc_fullness,
    check_r2_usage,
)
from checks_logs import (
    check_loki_ingestion,
    check_loki_reachable,
    check_promtail_dropped,
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


def apply_startup_grace(name, ok, msg, threshold, streaks):
    """Pure: hold a reach-out check `up` through the first `threshold`-1 consecutive down cycles.

    `streaks` is a name->consecutive-down-count dict, mutated in place. An `ok` result resets the
    count; a down result advances the shared `down_streak` hysteresis, so a held cycle reads with the
    same "down streak n/N" / "(n cycles)" wording as the HA/UPS/Discord per-check grace.
    """
    if ok:
        streaks[name] = 0
        return ok, msg
    streaks[name], ok, msg = bridge_streaks.down_streak(
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
    bridge_io.push(_env(push_env, ""), ok, msg)
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
            for job in down_exporters(
                bridge_io.prom_vector("up%s" % bridge_io.origin_sel())
            ):
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
        bridge_io.push(
            _env("KUMA_PUSH_CLUSTER_PROMETHEUS", ""), cluster_ok, cluster_msg
        )

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
        bridge_io.push(token, ok, msg)


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
