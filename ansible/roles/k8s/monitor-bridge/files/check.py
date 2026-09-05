#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

# This file is the registry and the run loop; every check body lives in a checks_* module.
# `main()` builds the frozen `Config` once and threads it down — see this role's CLAUDE.md,
# *Configuration is a parameter, not a module global*.
#
# A name the suite DOES patch is still read QUALIFIED from the module that binds it —
# `bridge.net.push`, `bridge.common.log` — never from-imported, because a from-import copies the
# value in at import time and never sees the patch. The check_* entries below ARE from-imported:
# run_once reads them from this module's globals and the gates tests patch them HERE. Enforced by
# ansible/tests/services/test_bridge_patch_boundary.py; the census of what is patched where is
# ansible/tests/services/test_monitor_bridge_modules.py.
import bridge.common
from bridge.common import _env

from bridge.config import Config, load_config
import bridge.net
import bridge.streaks
from checks.notify import (
    check_discord,
)
from checks.service import (
    check_arr_queue,
    check_bazarr,
    check_etcd_restore_drill,
    check_gitops_alive,
    check_gitops_status,
    check_staging_backfill_alive,
    check_ha_heartbeat,
    check_n8n,
    check_prowlarr_indexers,
)
from checks.cluster import (
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
from checks.host import check_cert, check_disk, check_mem
from checks.host_thermal import check_host_temp, check_scrutiny, check_ups
from checks.host_edge import check_pi_pressure, check_speedtest
from checks.b2 import (
    check_b2_reachable,
    check_b2_storage,
)
from checks.r2 import check_r2_usage
from checks.storage import (
    check_longhorn_volumes,
    check_pvc_fullness,
)
from checks.logs import (
    check_loki_ingestion,
    check_loki_reachable,
    check_shipper_dropped,
)


CheckFn = Callable[[Config], tuple[bool, str]]  # every check body and every gate


class CheckResult(NamedTuple):
    """What a check or a gate decided: `ok` and the message pushed to Kuma.

    A NamedTuple rather than a dataclass so every existing `ok, msg = fn()` unpacking — in this
    file, in the checks modules and across the test suite — keeps working unchanged. The check
    bodies themselves still return a plain `(bool, str)` tuple, which this type accepts; the name
    exists so the registry's consumption boundary here says what the pair means.
    """

    ok: bool
    msg: str


@dataclass(frozen=True)
class Check:
    """One entry in the CHECKS registry.

    Attributes:
      name: The check's own name — what CHECKS_ONLY/CHECKS_SKIP and the gate sets refer to.
      token: The Kuma push-monitor token this check's result is pushed to. Empty skips the push.
      fn: The check body. Takes the frozen `Config` and returns (ok, msg).
    """

    name: str
    token: str
    fn: CheckFn


# The push tokens are the ONE thing here still read from os.environ at import — the frozen
# Config covers every threshold and URL but not these. See the DECIDED note in main().
CHECKS = [
    Check("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    Check("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    Check("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
    # restarts/oom/cpu RETARGETED 2026-08-14 (Phase G): retired with the Docker cadvisor
    # the same morning, re-armed the same evening against the kubernetes-cadvisor job's
    # label shape — grouped by pod (`name` is the runtime hash there). Same pure logic,
    # same thresholds; complements k8s_workloads' crashloop paging with OOM + sustained-
    # throttle depth the retirement dropped.
    Check("restarts", _env("KUMA_PUSH_RESTARTS", ""), check_restarts),
    Check("oom", _env("KUMA_PUSH_OOM", ""), check_oom),
    Check("cpu", _env("KUMA_PUSH_CPU", ""), check_cpu_throttle),
    Check("targets", _env("KUMA_PUSH_TARGETS", ""), check_targets_down),
    Check("traefik5xx", _env("KUMA_PUSH_TRAEFIK", ""), check_traefik_5xx),
    Check(
        "traefik_latency",
        _env("KUMA_PUSH_TRAEFIK_LATENCY", ""),
        check_traefik_latency,
    ),
    Check("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n),
    Check("arr_queue", _env("KUMA_PUSH_ARR_QUEUE", ""), check_arr_queue),
    Check("bazarr", _env("KUMA_PUSH_BAZARR", ""), check_bazarr),
    Check(
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    Check("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    Check("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    # The staging-gate backfill ratchet's run-recency arm. Same shape and same hostPath as the
    # gitops pair above — it reads the unit's heartbeat out of /var/lib/gitops-deploy. Its
    # sibling `OnFailure=` unit pages when a run FAILS; this pages when runs stop happening.
    Check(
        "staging_backfill",
        _env("KUMA_PUSH_STAGING_BACKFILL", ""),
        check_staging_backfill_alive,
    ),
    # Reads a stamp the drill writes weekly rather than a live source, so it is the same shape
    # as the gitops pair above: a hostPath the pod is pinned to, read fail-closed. Its token was
    # minted 2026-08-28, which is what let it be registered — test_checks_and_env_secret_push
    # _tokens_agree blocks a check whose KUMA_PUSH_* name has no env-secret entry, correctly:
    # such a check pushes to nowhere forever, present in the code and absent from the world.
    Check(
        "etcd_restore_drill",
        _env("KUMA_PUSH_ETCD_DRILL", ""),
        check_etcd_restore_drill,
    ),
    Check("scrutiny", _env("KUMA_PUSH_SCRUTINY", ""), check_scrutiny),
    Check("host_temp", _env("KUMA_PUSH_HOST_TEMP", ""), check_host_temp),
    Check("ups", _env("KUMA_PUSH_UPS", ""), check_ups),
    Check("pi_pressure", _env("KUMA_PUSH_PI", ""), check_pi_pressure),
    Check("ha_heartbeat", _env("KUMA_PUSH_HA", ""), check_ha_heartbeat),
    Check("speedtest", _env("KUMA_PUSH_SPEEDTEST", ""), check_speedtest),
    Check("loki_ingestion", _env("KUMA_PUSH_LOKI", ""), check_loki_ingestion),
    Check(
        "shipper_dropped",
        _env("KUMA_PUSH_SHIPPER_DROPPED", ""),
        check_shipper_dropped,
    ),
    Check("discord", _env("KUMA_PUSH_DISCORD", ""), check_discord),
    Check("r2_usage", _env("KUMA_PUSH_R2_USAGE", ""), check_r2_usage),
    Check("b2_storage", _env("KUMA_PUSH_B2_STORAGE", ""), check_b2_storage),
    Check("k8s_workloads", _env("KUMA_PUSH_K8S_WORKLOADS", ""), check_k8s_workloads),
    Check(
        "cluster_targets", _env("KUMA_PUSH_CLUSTER_TARGETS", ""), check_cluster_targets
    ),
    Check(
        "longhorn_volumes",
        _env("KUMA_PUSH_LONGHORN_VOLUMES", ""),
        check_longhorn_volumes,
    ),
    Check("pvc_fullness", _env("KUMA_PUSH_PVC", ""), check_pvc_fullness),
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
        "shipper_dropped",  # increase() over both shippers' dropped-entries counters
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
# pages. Loki being UP but a shipper not shipping is a different signal Loki Log Ingestion still
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

# GATE_DEPENDENTS maps each reachability gate to the checks it suppresses when it is down. The
# CHECKS_ONLY/CHECKS_SKIP filter that names these lives in bridge/config.py, with the account of
# what the mechanism is for; validate_check_filter below is what refuses a filter that would
# disable a gate while leaving its dependents enabled.
GATE_DEPENDENTS = {
    "prometheus": PROM_DEPENDENT,
    "loki_reachable": LOKI_DEPENDENT,
    "b2_reachable": B2_DEPENDENT,
    "cluster_prometheus": CLUSTER_DEPENDENT,
}


def check_enabled(name: str, only: frozenset[str], skip: frozenset[str]) -> bool:
    """Is `name` enabled under the CHECKS_ONLY/CHECKS_SKIP filter?

    Args:
      name: The check name to test.
      only: The enable-exactly-this-set filter. Empty enables everything.
      skip: The names to drop.
    """
    if only and name not in only:
        return False
    return name not in skip


def validate_check_filter(
    only: frozenset[str], skip: frozenset[str], checks: list[Check]
) -> list[str]:
    """Pure: return the list of problems with a CHECKS_ONLY/CHECKS_SKIP configuration."""
    known = {c.name for c in checks} | set(GATE_DEPENDENTS)
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


def expand_gates_for_cli(names: frozenset[str]) -> frozenset[str]:
    """Union in every gate a `--check`-selected check depends on.

    `--check disk` alone must not trip validate_check_filter's "gate disabled but its dependents
    are enabled" refusal — the operator named the check they want, not the gate underneath it,
    and `--check` is a narrowing convenience rather than the strict CHECKS_ONLY env contract.
    CHECKS_ONLY keeps its existing all-or-nothing behaviour: an operator writing that env var is
    expected to spell out the gate themselves, same as today.

    Args:
      names: The check names passed on the command line via `--check`.
    """
    gates = set(names)
    for gate, dependents in GATE_DEPENDENTS.items():
        if names & dependents:
            gates.add(gate)
    return frozenset(gates)


def down_exporters(up_vector: list[tuple[dict, float]]) -> set[str]:
    """Pure: which EXPORTER_DEPENDENT jobs report up==0 in a Prometheus `up` vector.

    Fed prom_vector("up") — [(labels, value), ...]. Returns the subset of EXPORTER_DEPENDENT keys
    whose Prometheus job is down, so run_once can suppress their dependents. Unit-tested.
    """
    down_jobs = {m.get("job") for m, v in up_vector if v == 0}
    return {job for job in EXPORTER_DEPENDENT if job in down_jobs}


def _evaluate(cfg: Config, name: str, fn: CheckFn) -> CheckResult:
    """Runs one check, converting an unreachable source/metric into a descriptive `down`.

    Keeps the loop alive instead of letting an unreachable source or metric raise and
    kill it.
    """
    try:
        return CheckResult(*fn(cfg))
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return CheckResult(False, "%s check error: %s" % (name, e))


def _gate(
    cfg: Config,
    name: str,
    fn: CheckFn,
    push_env: str,
    dry_run: bool,
    only: frozenset[str],
) -> CheckResult:
    """Evaluate one reachability gate: verdict, log line, heartbeat push.

    A gate differs from an ordinary check only in what its verdict is used for — the CHECKS
    loop in run_once() reads it to suppress that gate's dependents, so a single outage pages
    once instead of storming. A disabled gate returns `True` so the filter suppresses nothing.

    Args:
      cfg: The gate body's config, and the skip filter. ASYMMETRIC with `only` on purpose:
        `--check` can narrow `only` per run, so run_once has a value the config does not carry;
        nothing narrows the skip set, so it is read off `cfg.CHECKS_SKIP` rather than threaded.
      name: The gate's own check name, as CHECKS_ONLY/CHECKS_SKIP and GATE_DEPENDENTS spell it.
      fn: The gate's check body.
      push_env: The env var holding this gate's Kuma push token.
      dry_run: Evaluate and log, but push nothing to Kuma.
      only: The enable-exactly-this-set filter.
    """
    if not check_enabled(name, only, cfg.CHECKS_SKIP):
        return CheckResult(True, "disabled by check filter")
    ok, msg = _evaluate(cfg, name, fn)
    bridge.common.log("OK  " if ok else "DOWN", name, "-", msg)
    if not dry_run:
        bridge.net.push(cfg, _env(push_env, ""), ok, msg)
    return CheckResult(ok, msg)


def run_once(
    cfg: Config, dry_run: bool = False, only: frozenset[str] | None = None
) -> None:
    """Runs one full check cycle: the reachability gates, then every enabled check.

    Evaluates the Prometheus, Loki, B2 and cluster-Prometheus gates first, so a single
    outage in one of them suppresses its dependent checks (pushed `up` with a skip
    message) instead of paging each of them separately. Every enabled check in CHECKS is
    then evaluated (unless suppressed by a gate or an exporter outage) and its result is
    logged and pushed to its Kuma monitor.

    Args:
      cfg: The frozen config `main()` built — the ONLY source of configuration in a cycle.
      dry_run: Evaluate and log every check, but push nothing to Kuma. Defaults to False, so
        the pod's own `python /app/check.py` and every existing caller are unchanged.
      only: The enable-exactly-this-set filter `--check` builds. None reads cfg.CHECKS_ONLY,
        which is what the pod runs with, so it must be threaded to EVERY check_enabled call
        below — a filter validated in main() and not passed here would print an enabled count
        it does not honour.
    """
    only = cfg.CHECKS_ONLY if only is None else only
    skip = cfg.CHECKS_SKIP
    # Prometheus reachability is evaluated FIRST and gates the prom-dependent checks: a single
    # Prometheus outage would otherwise page all of them at once (one root cause, an alert storm).
    # When it's down they're suppressed (pushed `up` with a skip msg, keeping each push monitor's
    # heartbeat alive) so only the Prometheus monitor pages; a real per-metric problem still alerts
    # whenever Prometheus is up.
    prom_ok, prom_msg = _gate(
        cfg, "prometheus", check_prometheus, "KUMA_PUSH_PROMETHEUS", dry_run, only
    )

    # Exporter-reachability gate (one level below the Prometheus gate): when Prometheus is up, probe
    # `up` once and suppress each dead exporter's dependents so a node-exporter/cadvisor death is one
    # page (Scrape Targets), not a 3-monitor false-page storm / silent-green split. A failure to
    # DETERMINE exporter health leaves `suppressed` empty (fail toward alerting, never masking).
    suppressed = set()
    if prom_ok and check_enabled("prometheus", only, skip):
        try:
            for job in down_exporters(
                bridge.net.prom_vector(cfg, "up%s" % bridge.net.origin_sel(cfg))
            ):
                suppressed |= EXPORTER_DEPENDENT[job]
        except Exception as e:
            bridge.common.log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (LOKI_DEPENDENT).
    loki_ok, _loki_msg = _gate(
        cfg,
        "loki_reachable",
        check_loki_reachable,
        "KUMA_PUSH_LOKI_REACHABLE",
        dry_run,
        only,
    )

    # B2-reachability gate (peer of the two above): B2 caps TRANSACTIONS separately from storage
    # bytes, and the kopia-era state-file checks this used to gate all reported their last
    # successful cron run rather than current B2 health — the 2026-08-02 transaction-cap incident.
    # Those checks are gone (backup moved to Longhorn), but b2_reachable stays: Longhorn still
    # needs B2. The probe is throttled inside b2_reachable (it must not spend the transaction
    # budget it is watching), but the cached verdict is pushed every cycle so this monitor's own
    # heartbeat stays alive.
    b2_ok, _b2_msg = _gate(
        cfg, "b2_reachable", check_b2_reachable, "KUMA_PUSH_B2_REACHABLE", dry_run, only
    )

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
    if check_enabled("cluster_prometheus", only, skip):
        # The same-instance reuse only holds when the prometheus gate actually probed.
        if (
            cfg.CLUSTER_PROM_URL
            and cfg.CLUSTER_PROM_URL == cfg.PROM_URL
            and check_enabled("prometheus", only, skip)
        ):
            cluster_ok, cluster_msg = (
                prom_ok,
                "same instance as the Prometheus gate (%s)" % prom_msg,
            )
        else:
            cluster_ok, cluster_msg = _evaluate(
                cfg, "cluster_prometheus", check_cluster_prometheus
            )
        bridge.common.log(
            "OK  " if cluster_ok else "DOWN", "cluster_prometheus", "-", cluster_msg
        )
        if not dry_run:
            bridge.net.push(
                cfg, _env("KUMA_PUSH_CLUSTER_PROMETHEUS", ""), cluster_ok, cluster_msg
            )

    for entry in CHECKS:
        name, token, fn = entry.name, entry.token, entry.fn
        if not check_enabled(name, only, skip):
            continue
        if not prom_ok and name in PROM_DEPENDENT:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not loki_ok and name in LOKI_DEPENDENT:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not b2_ok and name in B2_DEPENDENT:
            ok, msg = True, "skipped — B2 unreachable (see B2 Reachable monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not cluster_ok and name in CLUSTER_DEPENDENT:
            ok, msg = (
                True,
                "skipped — cluster Prometheus unreachable (see Cluster Prometheus monitor)",
            )
            bridge.common.log("SKIP", name, "-", msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see Scrape Targets)"
            bridge.common.log("SKIP", name, "-", msg)
        else:
            ok, msg = _evaluate(cfg, name, fn)
            if name in STARTUP_GRACE:
                ok, msg = bridge.streaks.apply_startup_grace(
                    name, ok, msg, cfg.GRACE_CYCLES, bridge.streaks._grace_streaks
                )
            bridge.common.log("OK  " if ok else "DOWN", name, "-", msg)
        if not dry_run:
            bridge.net.push(cfg, token, ok, msg)


def build_parser() -> argparse.ArgumentParser:
    """The command line. `python /app/check.py` with no arguments is the pod's invocation."""
    parser = argparse.ArgumentParser(
        prog="check.py",
        description=(
            "Evaluate the homelab health checks and push each result to its Uptime Kuma "
            "push monitor. With no arguments this loops forever at INTERVAL seconds, which "
            "is what the Deployment runs."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit, instead of looping at INTERVAL seconds",
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        dest="checks",
        help=(
            "run only this check; repeatable. Validated like CHECKS_ONLY — a gate whose "
            "dependents are enabled may not be disabled — but unlike CHECKS_ONLY, the gate "
            "each named check depends on is unioned in automatically."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate every check and print the results, but push nothing to Kuma",
    )
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Validates the configuration and the check filter, then runs the check loop.

    Returns 2 without running any check if bridge/config.py could not parse an env value, or if
    CHECKS_ONLY/CHECKS_SKIP names an unknown check or disables a gate whose dependents are still
    enabled. `--check` is validated the same way, but first has its named checks' gates unioned
    in (see expand_gates_for_cli) — CHECKS_ONLY keeps the strict env contract, `--check` does
    not need the gate spelled out alongside the check. Otherwise loops run_once() at
    cfg.INTERVAL seconds, touching the heartbeat file after every cycle, forever unless --once
    was given.

    Args:
      argv: The argument list, without the program name. None reads sys.argv.
      env: The environment `load_config` reads. None reads the real one. See the DECIDED note.

    Returns:
      The process exit code: 0 after a completed --once run, 2 on a configuration fault.
    """
    args = build_parser().parse_args(argv)
    # Building the config never raises; bridge.common.CONFIG_PROBLEMS carries HTTP_TIMEOUT's own
    # parse failure, parsed there because autofix-bridge shares that module.
    #
    # DECIDED: `env` reaches `load_config` and nothing else.
    # The CHECKS registry above still reads its KUMA_PUSH_* tokens from os.environ at import,
    # so `main(env={...})` cannot change which monitor a result is pushed to. Slice 17b moves
    # CHECKS to registry.py, where it can take the environment as a parameter; threading it
    # here would rebuild a module-level list inside main(), the global this seam removed.
    cfg = load_config(
        os.environ if env is None else env, problems=bridge.common.CONFIG_PROBLEMS
    )
    if cfg.CONFIG_PROBLEMS:
        for problem in cfg.CONFIG_PROBLEMS:
            bridge.common.log("FATAL: bad monitor-bridge config:", problem)
        return 2
    only = (
        expand_gates_for_cli(frozenset(args.checks)) if args.checks else cfg.CHECKS_ONLY
    )
    problems = validate_check_filter(only, cfg.CHECKS_SKIP, CHECKS)
    if problems:
        for p in problems:
            bridge.common.log("FATAL: bad CHECKS_ONLY/CHECKS_SKIP:", p)
        return 2
    enabled = [c.name for c in CHECKS if check_enabled(c.name, only, cfg.CHECKS_SKIP)]
    bridge.common.log(
        "monitor-bridge starting (interval=%ss, once=%s, dry_run=%s, checks=%d/%d)"
        % (cfg.INTERVAL, args.once, args.dry_run, len(enabled), len(CHECKS))
    )
    while True:
        run_once(cfg, dry_run=args.dry_run, only=only)
        # A --dry-run hand-run must touch nothing live, including the liveness-probe file — see
        # build_parser()'s --dry-run help.
        if not args.dry_run:
            bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
        if args.once:
            return 0
        time.sleep(cfg.INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
