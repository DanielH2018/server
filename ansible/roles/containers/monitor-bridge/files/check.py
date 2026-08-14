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
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def _env(name, default):
    return os.environ.get(name, default)


def _env_file(name, default=""):
    """Read a secret from the file named by <name>_FILE if set, else the plain <name> env var.

    Inlined in the compose environment, a secret lands in the container's Config.Env, which the
    read-only docker-proxy exposes to any monitoring-net neighbor. Pointing <name>_FILE at a
    0600 bind-mounted file keeps it out of container metadata (2026-07-15 review H2). Trailing
    whitespace is stripped so a rendered newline can't corrupt the value.

    A read error (the file went missing, or Docker auto-created the mount source as a directory
    because the host file was absent at container-create) falls back to the plain env var rather
    than raising: this runs at import for HA_TOKEN, so an unguarded open() would crash the whole
    loop and silence all monitors over one missing file, instead of just disabling the HA check
    the way an empty file does (2026-07-15 review L1).
    """
    path = os.environ.get(name + "_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return os.environ.get(name, default)


INTERVAL = int(_env("INTERVAL", "300"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
# Startup/redeploy grace for the reach-out checks (STARTUP_GRACE, applied in run_once). The
# bridge's first cycle after a host reboot runs before the heavy apps it polls (n8n,
# sonarr/radarr, prowlarr, scrutiny, the Pi glances) finish starting, so an un-graced reach-out
# check flips its
# max_retries=0 push monitor DOWN on that one transient cycle and pages, then recovers next cycle —
# the weekly-reboot noise. Like HA_CONSECUTIVE, only the GRACE_CYCLES'th consecutive down pages; a
# genuinely-down dependency still alerts after ~one extra INTERVAL, and one ok resets the streak.
GRACE_CYCLES = int(_env("GRACE_CYCLES", "2"))
# Touched after every completed cycle; the container healthcheck compares its mtime
# against ~3×INTERVAL. PID death already restarts the container, but a HANG only shows
# up as push silence in Kuma — the healthcheck lets autoheal restart on that too.
HEARTBEAT_FILE = _env("HEARTBEAT_FILE", "/tmp/heartbeat")
PROM_URL = _env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
LOKI_URL = _env("LOKI_URL", "http://loki:3100").rstrip("/")

DISK_MOUNTPOINTS = [
    m.strip() for m in _env("DISK_MOUNTPOINTS", "/").split(",") if m.strip()
]
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
# Slowness is measured at a histogram BUCKET BOUNDARY, not with histogram_quantile. Traefik's
# default buckets are 0.1 / 0.3 / 1.2 / 5.0 / +Inf, so between 1.2s and 5.0s there is nothing to
# interpolate from and a quantile landing there is invented, not measured. The old check compared
# histogram_quantile(0.95, ...) against 3s — a threshold sitting inside that empty 3.8s-wide gap.
# Measured on homepage@docker 2026-08-06 13:15-13:20 UTC: Prometheus reported p95 4.058s while the
# Traefik access log for the same window showed a real p95 of 1.576s (114 requests, 24 over 1.2s,
# max 3.063s). Every firing was that arithmetic. Across all 42,598 homepage requests that day only
# 8 exceeded 3s, never more than 2 in a 5-minute window, so no window's real p95 came near it.
#
# The bucket counts themselves are exact, so "more than 5% of requests exceeded 5.0s" IS "p95 above
# 5.0s", stated without interpolation. Keep TRAEFIK_SLOW_BUCKET on a boundary Traefik actually
# emits — an le= that matches no series selects nothing (see the unmeasurable branch below).
TRAEFIK_SLOW_BUCKET = _env("TRAEFIK_SLOW_BUCKET", "5.0")
TRAEFIK_SLOW_PCT = float(_env("TRAEFIK_SLOW_PCT", "5"))
N8N_URL = _env("N8N_URL", "http://n8n:5678").rstrip("/")
N8N_API_KEY = _env("N8N_API_KEY", "")
# n8n hides successful executions (EXECUTIONS_DATA_SAVE_ON_SUCCESS=none, kept that way to bound
# database.sqlite + its B2 backup churn), so "consecutive" can't be read from one snapshot — the
# per-workflow failure streak is accumulated across cycles in _n8n_streaks (see n8n_update_streaks):
# it advances once per NEW error (deduped by execution id) and resets when a workflow's latest
# error ages past N8N_FAIL_WINDOW (recovered / went idle).
N8N_FAIL_WINDOW = _env("N8N_FAIL_WINDOW", "2h")
N8N_CONSECUTIVE_MAX = int(_env("N8N_CONSECUTIVE_MAX", "3"))
# Systemic catch: if N8N_SYSTEMIC_MAX+ workflows are each failing >= N8N_SYSTEMIC_STREAK times,
# something is wrong with n8n itself — page now as ONE alert instead of waiting for each to reach
# N8N_CONSECUTIVE_MAX (and instead of a per-workflow flood).
N8N_SYSTEMIC_STREAK = int(_env("N8N_SYSTEMIC_STREAK", "2"))
N8N_SYSTEMIC_MAX = int(_env("N8N_SYSTEMIC_MAX", "2"))
_n8n_streaks = {}

# Sonarr/Radarr queue warnings: the 2026-07-01 incident — an indexer served a poisoned
# fake-episode .exe, sonarr itself blocked the import and flagged the queue item
# trackedDownloadStatus "warning" (message: "Caution: Found executable file with
# extension: '.exe'") — but nothing paged, so the release sat seeding for a full day
# before a manual review caught it. Polled directly (X-Api-Key header), same "internal
# REST API, empty key disables" idiom as N8N_API_KEY.
SONARR_URL = _env("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = _env("SONARR_API_KEY", "")
RADARR_URL = _env("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = _env("RADARR_API_KEY", "")

# Prowlarr sustained-indexer watchdog: Prowlarr's in-app health notification is binary — with
# warnings on every indexer flap pages, with warnings off only the all-indexers-down red error
# fires; there's no duration grace. We poll /api/v1/indexerstatus and go `down` only when an
# indexer has been FAILING for >= PROWLARR_INDEXER_MIN_DOWN_MIN (age from Prowlarr's own
# initialFailure, so it survives a monitor-bridge redeploy), suppressing the sub-threshold flaps
# public trackers throw that self-clear inside Prowlarr's ~5-15min backoff. Empty key = disabled
# (stays up), same idiom as N8N_API_KEY. Already on `media`, so prowlarr:9696 is reachable.
PROWLARR_URL = _env("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
PROWLARR_API_KEY = _env("PROWLARR_API_KEY", "")
PROWLARR_INDEXER_MIN_DOWN_MIN = float(_env("PROWLARR_INDEXER_MIN_DOWN_MIN", "30"))
# Comma-separated indexer names (case-insensitive) never counted as offenders. For chronically
# flaky PUBLIC trackers whose backend routinely 503s/times-out past the sustained-down gate (e.g. The Pirate
# Bay's apibay.org) — they'd page every outage though the other indexers cover the same searches.
# Prowlarr's own all-indexers-down onHealthIssue is the backstop if every indexer, ignored or not,
# fails at once. Empty = ignore nothing.
PROWLARR_INDEXER_IGNORE = _env("PROWLARR_INDEXER_IGNORE", "")
GITOPS_STATE_DIR = _env("GITOPS_STATE_DIR", "/gitops-state")
GITOPS_MAX_AGE_S = float(_env("GITOPS_MAX_AGE_MIN", "90")) * 60
# How long the host may sit behind origin before GitOps Status pages. Generous on purpose: the
# deployer ticks every 30 min, and the dirty-tree path (operator mid-edit) is behind by design for
# as long as the edit lasts. 6 h pages a genuinely-stuck host well inside a day while never firing
# on a normal push or a long editing session.
GITOPS_BEHIND_MAX_S = float(_env("GITOPS_BEHIND_MAX_MIN", "360")) * 60
RENOVATE_STATE_DIR = _env("RENOVATE_STATE_DIR", "/renovate-state")
RENOVATE_MAX_AGE_S = float(_env("RENOVATE_MAX_AGE_MIN", "2160")) * 60

# Daily wg-easy Pi-peer backup pull: the wg-easy role's daniel-server host cron
# (wg-easy-pull-pi-peers.sh) rsyncs the Pi's WireGuard peer configs (wg0.conf/wg0.json — private
# keys a redeploy can't rebuild) into Kopia scope and writes {"ts": epoch, "ok": bool, "msg": str}.
# It's the only Pi state pulled into the backup AND was the only backup cron with no watchdog: the
# pull uses no --delete, so a broken pull (Pi unreachable, SSH/sudo break) leaves the last-good copy
# in place while the peers silently go stale. We alert on a FAILED
# pull, staleness (cron broken / never ran), or a missing/corrupt state file. 2.5d staleness = two
# missed daily runs + slack.
PI_PEERS_STATE = _env("PI_PEERS_STATE", "/pi-peers/state.json")
PI_PEERS_MAX_AGE_S = float(_env("PI_PEERS_MAX_AGE_D", "2.5")) * 86400

# Every-5-min CrowdSec home-IP allowlist updater (traefik role's crowdsec-update-home-allowlist.sh):
# keeps the operator's current home public IP in CrowdSec's `home-ips` allowlist so the public path
# from home doesn't trip the WAF. It writes {"ts": epoch, "ok": bool, "msg": str} on EVERY run (incl.
# the common IP-unchanged fast path). It was the last self-`logger`ing cron with no watchdog — a silent
# failure (ipify unreachable, cscli error) just meant occasional 403s on the next IP rotation, invisible
# until noticed. We alert on a FAILED run or staleness (cron broken / never ran). 30 min = 6 missed
# 5-min runs; the fast-path heartbeat keeps a healthy no-op green.


# Hourly disk-autoprune host cron (autofix-bridge role): writes {"ts": epoch, "ok": bool, "msg":
# str} after checking `/` used% against a threshold and, if crossed, running a conservative
# docker/builder/container prune. Same state-file idiom as pi_peers. ok=false means the
# prune command itself errored — a disk still full of real data after a clean prune is Root
# Disk's alert, not this one. 3h staleness = 3x the hourly cron + slack.
DISK_PRUNE_STATE = _env("DISK_PRUNE_STATE", "/autofix-disk/state.json")
DISK_PRUNE_MAX_AGE_S = float(_env("DISK_PRUNE_MAX_AGE_H", "3")) * 3600

# B2 REACHABILITY — the gap the 2026-08-02 transaction-cap incident exposed
# (docs/b2-transaction-cap-monitoring-gaps.md). B2 caps TRANSACTIONS separately from storage
# bytes; the kopia-era state-file checks this used to gate reported their last successful cron
# run rather than current B2 health, so all of them read green — "B2 6.05/10GB billable (60% of
# plan)" among them — for nine and a half hours while B2 refused every request. Worse than absent
# — an operator triaging the one true alert was told by these that B2 was fine. Those checks were
# removed 2026-08-10 (kopia is retired, backup moved to Longhorn — see
# docs/k3s-migration/backup-consolidation-longhorn.md), but this probe stays: Longhorn still needs
# B2 reachable.
#
# The probe authenticates against B2's native API. A cap breach answers b2_authorize_account with
# HTTP 403 and error code `transaction_cap_exceeded`, which _get_json's HTTPError detail carries
# verbatim into the alert message, naming the cause directly.
#
# ASSUMPTION, stated because it is load-bearing and cannot be tested without a live breach:
# that b2_authorize_account is itself subject to the cap. Backblaze's endpoint documentation lists
# 403/transaction_cap_exceeded among its errors, which is the basis. If a future breach shows THIS
# monitor stayed green through it, the assumption was wrong — point B2_PROBE_URL
# at a Class C call (b2_list_buckets, which needs the account id from the auth response) instead.
# It is a URL swap, not a rewrite, deliberately.
B2_PROBE_URL = _env(
    "B2_PROBE_URL", "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
)
# Read through _env_file for the same reason as HA_TOKEN: inlined in the compose environment a
# secret lands in the container's Config.Env, which the read-only docker-proxy exposes to any
# monitoring-net neighbor (2026-07-15 review H2). Named B2_PROBE_* rather than KOPIA_B2_* even
# though the compose currently feeds it Kopia's credentials — this probe only needs to
# authenticate, so swapping in a scoped read-only key later is an inventory edit, not a code one.
B2_PROBE_KEY_ID = _env_file("B2_PROBE_KEY_ID")
B2_PROBE_APPLICATION_KEY = _env_file("B2_PROBE_APPLICATION_KEY")
# Probe at most this often, and cache BOTH outcomes until it expires. Every other gate re-probes
# each cycle; this one must not, because the failure it detects is a transaction cap and an
# uncached probe would add 288 calls/day (INTERVAL=300) to the very budget it is watching. Caching
# only successes — the EMAIL_PROBE_INTERVAL_S idiom — would be worse than useless here: it retries
# on failure, so a cap breach would drive the full 288 rejected calls/day into an exhausted cap.
# At 1800s this is 48 calls/day flat, and detection lands within 30 min of a breach that last time
# went 9.5 hours unactioned.
B2_PROBE_INTERVAL_S = float(_env("B2_PROBE_INTERVAL_S", "1800"))

# k3s workload health, via the CLUSTER's Prometheus — a SECOND Prometheus, not the one PROM_URL
# points at. Slice 3 D8 (docs/k3s-migration/slice-3-monitoring-plane.md). Seven k8s workloads ran
# from slice 2 with no monitor of any kind, n8n-runners among them, because none of them is
# probeable from here: three expose only a ClusterIP, four expose no Service at all, and none has
# an ingress route. Their health is a Kubernetes API property, so kube-state-metrics is the only
# thing that can express it as something this bridge can query.
#
# Reached over the cluster ingress at prometheus-k8s.local.<domain>, whose IngressRoute admits only
# /api/v1/query. Not the ClusterIP (unreachable from this host) and not the node's :9090, which is
# pinned to the cluster node's loopback. Empty = disabled (stays up), like N8N_API_KEY.
CLUSTER_PROM_URL = _env("CLUSTER_PROMETHEUS_URL", "").rstrip("/")

# Which estate the host-health checks mean, when one Prometheus holds two (slice 3, B5).
#
# Since B3 the cluster Prometheus carries daniel-server's whole TSDB alongside its own, tagged with
# `origin` (Prometheus external_labels). Three metric families genuinely exist on BOTH sides —
# measured 2026-08-07: container_start_time_seconds (99 cluster-native / 53 here),
# container_memory_failcnt and the container_cpu_cfs_* pair, plus `up` (5 / 11). So the moment
# PROMETHEUS_URL points at the cluster, restarts / oom / cpu / janitorr / targets silently widen
# from "daniel-server's containers" to "every container in the homelab", and would start naming k8s
# pods as offenders. The remaining PROM_DEPENDENT checks that DON'T read traefik_* are pinned
# (disk/cert/memory/restarts/oom/cpu/targets/ups/promtail_dropped). Since E2 the cluster edge also
# emits traefik_* (traefik-k8s job), so the unpinned traefik/cert checks now deliberately read the
# CLUSTER edge's metrics.
#
# `{name!=""}` does NOT already scope this, which is the obvious assumption and a wrong one: the
# kubelet's cAdvisor emits `name` too, so 99 cluster-native series survive that filter.
#
# DERIVED, not configured. The pin is required when reading the cluster copy and WRONG when
# reading the Docker instance — whose own storage has no `origin` label at all, because
# external_labels are applied on remote-write and never to local queries. A compose variable that
# had to be flipped in lockstep with PROMETHEUS_URL is precisely the drift this avoids: pointing
# one at the cluster and forgetting the other would silently select nothing and read as healthy.
# The _env override stays so a third estate is not blocked by the derivation.
PROM_ORIGIN = _env(
    "PROM_ORIGIN",
    'origin="daniel-server"' if PROM_URL and PROM_URL == CLUSTER_PROM_URL else "",
)

# Floor below which the `up` vector is treated as missing rather than clean — see
# targets_verdict. The cluster prometheus scrapes exactly three origin="daniel-server" jobs
# since the Phase F drain retired the demoted crowdsec agent's 9103 (2026-08-13; its
# successor, the crowdsec-node-agent DaemonSet, is scraped per-pod under the cluster-native
# set): node, cadvisor, promtail. promtail STAYS a Docker-host tailer until its own drain
# slot, so this floor of 3 holds until then (2 after).
TARGETS_MIN = int(_env("TARGETS_MIN", "3"))
# Same floor idea for the cluster's own scrape targets (see check_cluster_targets). Since the
# otel-collector became a DaemonSet (Phase F drain, 2026-08-13) its two jobs are per-POD —
# one target per node each — so the set is seven: prometheus, 2x otel-collector,
# 2x otel-collector-internal, kube-state-metrics, kubernetes-cadvisor. 3 still tolerates a
# deliberate removal without ever mistaking an empty vector for a clean one.
CLUSTER_TARGETS_MIN = int(_env("CLUSTER_TARGETS_MIN", "3"))


def origin_sel(*matchers):
    """A `{...}` label-matcher block: the given matchers plus the origin pin, when one applies.

    Returns "" when there is nothing to select on, so `"up%s" % origin_sel()` is a bare `up`
    against the Docker Prometheus and `up{origin="daniel-server"}` against the cluster copy.
    """
    parts = [m for m in matchers if m]
    if PROM_ORIGIN:
        parts.append(PROM_ORIGIN)
    return "{%s}" % ", ".join(parts) if parts else ""


# The floor below which the deployment series is treated as missing rather than healthy. THE
# FAILURE THIS EXISTS TO PREVENT: an absent series makes `unavailable > 0` return an empty vector,
# which reads exactly like "nothing is unavailable" — green, silent, and wrong, the same shape as
# the B2 transaction cap (2026-08-02) and the gitops-behind defer (2026-08-07). So the check
# COUNTS the series first and fails closed when the count is short, instead of inferring health
# from an empty result. The floor also covers a partially-loaded kube-state-metrics: its
# ClusterRole is deliberately scoped, so dropping `apps` from it would take every deployment series
# away while the pod stays up and Ready.
K8S_MIN_WORKLOADS = int(_env("K8S_MIN_WORKLOADS", "5"))
# Same fail-closed reasoning as K8S_MIN_WORKLOADS, for the DaemonSet series
# (kube_daemonset_status_number_unavailable) instead of the Deployment one — a DaemonSet's
# absent/unschedulable pod has no Deployment-arm equivalent, so it was invisible until this
# arm existed. The nine DaemonSets running as of 2026-08-13: otel-collector, promtail,
# scrutiny-collector, crowdsec-node-agent, dri-device-plugin, engine-image-*,
# longhorn-csi-plugin, longhorn-manager, speaker. Bump this floor (and the comment) when a
# DaemonSet is added or retired — same discipline as K8S_MIN_WORKLOADS.
K8S_MIN_DAEMONSETS = int(_env("K8S_MIN_DAEMONSETS", "9"))
# Crash-loop arm of the workload check: pods whose restart counter climbed more than
# K8S_RESTART_MAX inside K8S_RESTART_WINDOW page even while readiness flaps green
# (CrashLoopBackOff passes probes briefly each backoff cycle — the 2026-08-13 homepage
# incident: 31 restarts overnight, tile and replica check mostly green throughout).
# 3-in-1h ≈ steady-state backoff cadence; a legitimate deploy rollout restarts once.
K8S_RESTART_WINDOW = _env("K8S_RESTART_WINDOW", "1h")
K8S_RESTART_MAX = int(_env("K8S_RESTART_MAX", "3"))

# Scrutiny SMART freshness + health: the collector cron runs daily (00:00) and has no usable
# container healthcheck (cron is PID 1) — a silently-dead collector only shows as aging
# collector_date values in the web API. 26h allows one run + slack. On TOP of freshness we assert
# each device's `device_status` == 0: freshness only proves the collector still reports, so a drive
# that goes SMART-FAILED / breaches a Scrutiny attribute threshold while STILL reporting fresh data
# would otherwise page nothing (Scrutiny stores to InfluxDB, not Prometheus, and its own Shoutrrr
# notifier is unconfigured — this bridge check is the only alert path). SCRUTINY_TEMP_MAX is an
# optional temperature ceiling (°C); 0 = disabled (default), since Scrutiny already folds the SMART
# temperature attribute into device_status — the ceiling is just an earlier-warning lever.
SCRUTINY_URL = _env("SCRUTINY_URL", "http://scrutiny:8080").rstrip("/")
SCRUTINY_MAX_AGE_H = float(_env("SCRUTINY_MAX_AGE_H", "26"))
SCRUTINY_TEMP_MAX = float(_env("SCRUTINY_TEMP_MAX", "0"))

# UPS battery health via Home Assistant's Prometheus scrape (the APC UPS is on NUT/peanut; HA's
# prometheus integration exposes its sensors as hass_sensor_*). The only pre-existing UPS alert is
# an HA automation -> mobile push (a separate channel from this Kuma->Discord brain), and nothing
# trends the battery, so a slowly-degrading battery — full-charge runtime decaying over years — is
# invisible until an outage collapses it. We page on a low battery RUNWAY: charge below
# UPS_CHARGE_MIN_PCT (a deep discharge while on battery) OR estimated runtime below UPS_RUNTIME_MIN_S
# (an aged battery even at full charge, or a discharge nearing shutdown) — a dual-purpose health +
# imminent-cutoff floor — PLUS the UPS's own replace-battery self-test verdict (UPS_REPLACE_QUERY),
# the earliest signal, which can trip while charge/runtime still read fine. Queries are env-driven
# (all empty = disabled, like PI_GLANCES_URL) so a UPS/entity rename or removal needs no code edit.
# Prom-dependent: an HA-scrape outage leaves ALL series absent -> up (Scrape Targets owns HA-source
# liveness; the nut container healthcheck owns NUT-server death), so this never double-pages those;
# a PARTIAL drop (one arm gone) pages instead of silently monitoring the survivor. UPS_CONSECUTIVE
# rides out a one-cycle dip from a transient load spike (like HA_CONSECUTIVE), so only a sustained
# problem pages.
UPS_CHARGE_QUERY = _env(
    "UPS_CHARGE_QUERY",
    'hass_sensor_battery_percent{entity="sensor.apc_ups_battery_charge"}',
)
UPS_RUNTIME_QUERY = _env(
    "UPS_RUNTIME_QUERY",
    'hass_sensor_duration_s{entity="sensor.apc_ups_battery_runtime"}',
)
# The UPS's own "Replace Battery" self-test verdict (NUT `ups.status` RB flag). Charge/runtime are a
# lagging runway proxy — a failed periodic self-test can trip RB while both still read fine — so this
# is the earliest actionable replace-the-battery signal, and it reached NEITHER alert channel before
# (the HA ups_power_event automation only branches on OB/LB, and check_ups read only charge/runtime).
# Exposed as a numeric 0/1 series by an HA template binary_sensor (home-assistant templates.yaml),
# which stays on/off — never unknown — while HA is up, so its absence means the whole HA scrape is
# down (all arms absent -> defer), not a silent single-arm drop. Empty = arm disabled.
UPS_REPLACE_QUERY = _env(
    "UPS_REPLACE_QUERY",
    'hass_binary_sensor_state{entity="binary_sensor.apc_ups_replace_battery"}',
)
# HA's own scrape-up series, used only to discriminate the all-arms-absent case: HA's whole
# Prometheus scrape being down (all hass_sensor_* vanish → Scrape Targets owns it, defer) vs HA
# scraping fine while every UPS entity was renamed/removed at once (Scrape Targets can't see it →
# the UPS would go silently unmonitored). Empty disables the gate (always defer, the old behaviour).
UPS_HA_UP_QUERY = _env("UPS_HA_UP_QUERY", 'up{job="home-assistant"}')
UPS_CHARGE_MIN_PCT = float(_env("UPS_CHARGE_MIN_PCT", "50"))
UPS_RUNTIME_MIN_S = float(_env("UPS_RUNTIME_MIN_S", "300"))
UPS_CONSECUTIVE = int(_env("UPS_CONSECUTIVE", "2"))

# Loki log-ingestion freshness: Loki's Kuma /ready probe stays green even when promtail
# stops SHIPPING (DOCKER_HOST/docker-proxy break, positions-file corruption, relabel
# regression) — a silently-dead log pipeline that quietly blinds the log dashboards and
# any future log forensics. Two arms, down if EITHER is silent:
#   arm 1 (file-tail union): count the file-tailed streams (authlog+syslog+traefik) over a
#   TOLERANT window (LOKI_FILETAIL_WINDOW) and go down at zero — a promtail static_configs
#   regression, a stale /var/log bind, or host rsyslog dying silences all three at once
#   (exactly what /ready can't see), while syslog's routine volume keeps the union alive on
#   a quiet night so no single low-volume file going quiet trips it. The selector EXCLUDES
#   the docker_sd stream (promtail stamps it `job: docker`, so a bare `{job=~".+"}` would
#   swallow it): that stream dwarfs the file-tail streams — ~all 44 containers' stdout — so
#   including it let a healthy docker stream MASK a total file-tail outage (arm 1 could then
#   only reach zero if promtail was TOTALLY dead, which arm 2 already catches — the
#   2026-07-07 blind-spot review). The window is wider than arm 2's because file-tail volume
#   is low and dips overnight (a lone `{job="syslog"}` over 10m false-paged 2026-06-23 —
#   this debloated host routinely idles >15m between syslog writes).
#   arm 2 (docker stream): count {container=~".+"} — the docker_sd stream carries a
#   `container` label, no `job`, so it's exactly the one arm 1 excludes. A docker_sd-specific
#   break (docker-proxy down, the docker relabel block regressing) silences every container
#   log while the file-tail streams keep flowing; a tight window catches a total promtail
#   death fast. Reached at loki:3100 over `monitoring`.
LOKI_STREAM = _env("LOKI_STREAM", '{job=~"authlog|syslog|traefik"}')
LOKI_DOCKER_STREAM = _env("LOKI_DOCKER_STREAM", '{container=~".+"}')
LOKI_WINDOW = _env("LOKI_WINDOW", "30m")
LOKI_FILETAIL_WINDOW = _env("LOKI_FILETAIL_WINDOW", "3h")

# Promtail dropped-entries watchdog: Prometheus scrapes promtail:9080, which exposes the
# promtail_dropped_entries_total{reason=...} counter. Loki Log Ingestion only catches TOTAL silence;
# this surfaces PARTIAL loss — entries promtail gave up shipping. NO reason filter (was
# reason="ingester_error" only): every reason is a real drop, and Loki's own configured limits
# reject under DIFFERENT reasons the ingester_error-only selector missed entirely — rate_limited
# (per_stream_rate_limit / ingestion_rate_mb), stream_limited (max_global_streams_per_user), and
# line_too_long — so a stream explosion or a chatty container hitting the rate cap dropped logs while
# this stayed green (2026-07-15 review M2). increase() over a window handles counter resets; alert
# only ABOVE a threshold so a transient Loki restart's handful of drops doesn't page. Prom-dependent
# (suppressed under the Prometheus gate). No series (counter never incremented) reads as 0 -> up; a
# dead promtail scrape is Scrape Targets' page, not this one.
PROMTAIL_DROPPED_SELECTOR = _env(
    "PROMTAIL_DROPPED_SELECTOR",
    "promtail_dropped_entries_total",
)
PROMTAIL_DROPPED_WINDOW = _env("PROMTAIL_DROPPED_WINDOW", "1h")
PROMTAIL_DROPPED_MAX = float(_env("PROMTAIL_DROPPED_MAX", "1000"))


def duration_seconds(spec):
    """Seconds in a Prometheus duration like `15m` / `2h` / `90s` / `1d`. Unit-tested."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    spec = spec.strip()
    if not spec or spec[-1] not in units or not spec[:-1].isdigit():
        raise ValueError("not a Prometheus duration: %r" % spec)
    return int(spec[:-1]) * units[spec[-1]]


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
# File-mounted (HA_TOKEN_FILE) so this full-access HA long-lived token stays out of the container
# Env the docker-proxy exposes to monitoring-net neighbors; falls back to the HA_TOKEN env.
HA_TOKEN = _env_file("HA_TOKEN", "")
HA_HEARTBEAT_MAX_AGE_S = float(_env("HA_HEARTBEAT_MAX_AGE", "300"))
HA_HEARTBEAT_ENTITY = "input_datetime.ha_heartbeat"
# Consecutive-cycle hysteresis (like CPU_CONSECUTIVE) so a planned HA redeploy — which takes
# the API unreachable for ~120s and then leaves the scheduler a beat behind — doesn't page.
# 2 straight down cycles (~one full INTERVAL of continuous badness) before `down`.
HA_CONSECUTIVE = int(_env("HA_CONSECUTIVE", "2"))

# Discord delivery: Kuma fires every alert by POSTing to its Discord webhook
# (monitor_discord_webhook_url). A rotated/revoked/deleted webhook leaves every monitor
# green-in-UI while Discord goes silent — the one link in the alert chain no other monitor
# (not even the off-box UptimeRobot host dead-man) verifies. We GET-verify the webhook is
# still valid: Discord answers a webhook GET with its JSON metadata + HTTP 200 while it
# exists and 404s once it's gone — a GET, not a POST, so this never puts a test message in
# the channel. Empty URL = disabled (stays up), like N8N_API_KEY. The streak hysteresis
# (like HA_CONSECUTIVE) rides out a transient blip on the one check that reaches the public
# internet.
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", "")
# The CrowdSec ban-alert webhook is a SECOND, independent Discord delivery hop: CrowdSec POSTs
# directly to it (not via Kuma), so a rotated/revoked CrowdSec webhook silently drops security-ban
# notifications with NO Kuma backstop. Verify it alongside the Kuma webhook. Empty = not checked.
DISCORD_CROWDSEC_WEBHOOK_URL = _env("DISCORD_CROWDSEC_WEBHOOK_URL", "")
# The GitOps/Renovate webhook is a THIRD independent hop: it delivers both the gitops-deploy
# rollback alert AND every renovate_notify manual-action digest, neither via Kuma. renovate_notify
# writes its "alive" liveness marker on every clean run regardless of whether the Discord POST
# succeeded, so a rotated/revoked webhook here leaves the Renovate Notifier — Alive monitor GREEN
# while every digest silently drops. Verify it too. Empty = not checked.
DISCORD_GITOPS_WEBHOOK_URL = _env("DISCORD_GITOPS_WEBHOOK_URL", "")
# The *arr health/event webhook is a FOURTH independent hop: Sonarr/Radarr/Prowlarr POST their own
# onHealthIssue alerts (indexer down, download-client errors, app DB errors — signals the Arr Queue
# check does NOT cover) directly to it via their in-app Discord "Connect", not via Kuma. A rotated/
# revoked webhook silently drops those while every container-up monitor stays green. Empty = not
# checked. (The URL lives only in the *arr app DBs + SOPS — this GET-verify is its one watchdog.)
DISCORD_ARR_WEBHOOK_URL = _env("DISCORD_ARR_WEBHOOK_URL", "")
# The healthchecks.io app's own Discord webhook is a FIFTH independent hop: healthchecks POSTs its
# own check-down/up alerts to it via a "webhook" notification channel (config lives only in
# hc.sqlite, not templated), NOT via Kuma. A rotated/revoked URL silently drops those. It's a
# redundant secondary path (healthchecks' primary alert route is SMTP email, and it self-logs send
# failures in hc.sqlite), but it's still an un-Kuma'd delivery hop worth verifying. Empty = skipped.
DISCORD_HEALTHCHECKS_WEBHOOK_URL = _env("DISCORD_HEALTHCHECKS_WEBHOOK_URL", "")
DISCORD_CONSECUTIVE = int(_env("DISCORD_CONSECUTIVE", "2"))

# Alert-email backstop deliverability (folded into check_discord). The uptime-kuma `email` notification
# (Gmail SMTP) is the independent 2nd channel attached ONLY to the Discord Delivery monitor — the
# escape hatch when the Kuma Discord webhook is dead (the alert-delivery SPOF). But it had no liveness
# check of its own, so a silently revoked Gmail app-password could leave that backstop dead undetected
# and BOTH channels down at once. We fold a throttled SMTP login probe into check_discord: connect +
# AUTH to SMTP_HOST:SMTP_PORT with the same creds Kuma uses, so a revoked password / broken SMTP flips
# the Discord Delivery monitor down (which still pages via the working Discord channel). Throttled to
# EMAIL_PROBE_INTERVAL_S — Gmail flags frequent AUTHs, so a success is cached and only a failure
# re-probes every cycle. Empty SMTP_PASSWORD = disabled (stays up), like the empty-webhook skips.
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "465"))
SMTP_USER = _env("SMTP_USER", "")
SMTP_PASSWORD = _env("SMTP_PASSWORD", "")
EMAIL_PROBE_INTERVAL_S = float(_env("EMAIL_PROBE_INTERVAL_S", "21600"))  # 6h


# --- HTTP / parsing helpers (pure-ish, unit-tested) -------------------------


FETCH_BODY_MAX = 180


def endpoint_label(url):
    """host:port for `url` — deliberately NOT the path or query.

    This ends up in Kuma messages and therefore in Discord. `_get_json` is used for the
    Discord webhook probe, whose URL carries the webhook token IN THE PATH, so including
    the path would publish that token to the very channel it authenticates. Some *arr
    callers put keys in headers rather than the URL, but host:port is enough to name the
    service either way, which is the whole point.
    """
    netloc = urllib.parse.urlsplit(url).netloc
    return netloc.rsplit("@", 1)[-1] or "unknown host"


def describe_fetch_failure(url, exc, body=""):
    """Compose the message an unreachable or erroring HTTP source should page with.

    `_evaluate` otherwise renders a bare `str(exc)`, which for the common failures is close
    to content-free: a socket timeout stringifies to just "timed out", naming neither the
    endpoint nor the service. The 2026-08-02 B2 transaction-cap outage paged for 13h as
    `backup check error: timed out` — indistinguishable from a Kopia hiccup, while the real
    cause ("Transaction cap exceeded") sat in Kopia's own log.

    Where the server did answer, its error body carries that cause, and urllib's HTTPError
    discards it unless read explicitly — so the body is the most valuable part when present.
    """
    where = endpoint_label(url)
    detail = " ".join((body or "").split())
    if detail:
        return "%s: %s: %s" % (where, exc, detail[:FETCH_BODY_MAX])
    return "%s: %s" % (where, exc)


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

    `base` selects which Prometheus: the default (PROM_URL) is the Docker one every
    PROM_DEPENDENT check reads. CLUSTER_PROM_URL is a genuinely different instance with a
    different reachability gate — see check_k8s_workloads.
    """
    result = _instant_query(base or PROM_URL, "/api/v1/query", promql, source)
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
        for series in _instant_query(base or PROM_URL, "/api/v1/query", promql, source)
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


# --- checks: each returns (ok, msg) -----------------------------------------


def check_disk():
    breaching = []
    for mp in DISK_MOUNTPOINTS:
        sel = '{mountpoint="%s"}' % mp
        # max() collapses any duplicate device/fstype series for the same mountpoint to one
        # deterministic value (duplicates share the value), so prom_scalar's result[0] order
        # can't matter.
        avail = prom_scalar("max(node_filesystem_avail_bytes" + sel + ")")
        size = prom_scalar("max(node_filesystem_size_bytes" + sel + ")")
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
    vec = prom_vector(
        "changes(container_start_time_seconds%s[%s])"
        % (origin_sel('name!=""'), RESTART_WINDOW)
    )
    offenders = _top_offenders(vec, "name", lambda v: v > RESTART_MAX)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) restarting >%.0fx in %s: %s" % (
            len(offenders),
            RESTART_MAX,
            RESTART_WINDOW,
            desc,
        )
    return True, "no restart loops in %s" % RESTART_WINDOW


def check_oom():
    """Containers OOM-killed within OOM_WINDOW, naming each one.

    Closes the loop on the per-container memory limits (deploy.resources). If cAdvisor
    doesn't expose container_oom_events_total the query is empty and this stays green.
    """
    vec = prom_vector(
        "sum(increase(container_oom_events_total%s[%s])) by (name)"
        % (origin_sel('name!=""'), OOM_WINDOW)
    )
    offenders = _top_offenders(vec, "name", lambda v: v > 0)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) OOM-killed in %s: %s" % (
            len(offenders),
            OOM_WINDOW,
            desc,
        )
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
    sel = origin_sel('name!=""')
    ratio_vec = prom_vector(
        "sum(rate(container_cpu_cfs_throttled_periods_total%s[%s])) by (name) "
        "/ sum(rate(container_cpu_cfs_periods_total%s[%s])) by (name)"
        % (sel, CPU_WINDOW, sel, CPU_WINDOW)
    )
    lost_cores = dict(
        (m.get("name", "?"), v)
        for m, v in prom_vector(
            "sum(rate(container_cpu_cfs_throttled_seconds_total%s[%s])) by (name)"
            % (sel, CPU_WINDOW)
        )
    )
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
        "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5]
    )
    if _cpu_breach_streak < CPU_CONSECUTIVE:
        return True, "throttling streak %d/%d (not alerting yet): %s" % (
            _cpu_breach_streak,
            CPU_CONSECUTIVE,
            desc,
        )
    return (
        False,
        "%d container(s) CPU-throttled >%.0f%% & >%.2f cores for %d cycles: %s"
        % (
            len(offenders),
            CPU_THROTTLE_PCT,
            CPU_MIN_THROTTLED_CORES,
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


def targets_verdict(vec, min_targets):
    """Pure: (ok, msg) from an `up` vector, failing closed when too few targets are visible.

    THE HOLE THIS CLOSES, opened by B5. Before the repoint an empty `up` could only mean the
    Prometheus being queried was down, and the PROM_DEPENDENT gate suppressed this check before it
    ran. Pointed at the cluster copy those two facts come apart: the gate probes the CLUSTER, which
    is up and answering, while `up{origin="daniel-server"}` goes empty the moment daniel-server's
    Prometheus stops remote-writing. `len(down) == 0` is then trivially true and this reports
    "all 0 targets up" — green, and blind to an entire estate having vanished.

    Same fail-closed shape as the k8s workload floor: count first, and treat "fewer series than
    could possibly be right" as UNKNOWN rather than healthy. This check is also the sentinel for
    the other estate-pinned checks — restarts/oom/cpu legitimately return empty when nothing is
    wrong, so they cannot tell "quiet" from "gone", and this one can.
    """
    if len(vec) < min_targets:
        return False, (
            "only %d scrape targets visible, below the floor of %d — the metrics estate is "
            "missing, so target health is UNKNOWN, not OK" % (len(vec), min_targets)
        )
    down = sorted({m.get("job") or m.get("instance") or "?" for m, v in vec if v == 0})
    if down:
        return False, "%d target(s) down: %s" % (len(down), ", ".join(down))
    return True, "all %d targets up" % len(vec)


def check_targets_down():
    """Any Prometheus scrape target reporting up==0 (monitoring going blind)."""
    return targets_verdict(prom_vector("up%s" % origin_sel()), TARGETS_MIN)


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
            len(offenders),
            TRAEFIK_5XX_PCT,
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
            "by (service)" % TRAEFIK_SLOW_BUCKET
        )
    )
    offenders = []
    unmeasurable = []
    eligible = 0
    worst = 0.0
    for svc, rps in total.items():
        if rps < TRAEFIK_MIN_RPS:
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
        if pct > TRAEFIK_SLOW_PCT:
            offenders.append((svc, pct, rps))
    if unmeasurable:
        return (
            False,
            "no %ss bucket for %d service(s) (%s) — check Traefik's histogram buckets"
            % (
                TRAEFIK_SLOW_BUCKET,
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
                TRAEFIK_SLOW_PCT,
                TRAEFIK_SLOW_BUCKET,
                desc,
            ),
        )
    return True, "latency ok: %d service(s) above floor, worst %.1f%% over %ss" % (
        eligible,
        worst,
        TRAEFIK_SLOW_BUCKET,
    )


def n8n_update_streaks(workflows_json, executions_json, state, now, window_s):
    """Advance per-workflow consecutive-failure streaks across check cycles.

    n8n doesn't record successful executions (EXECUTIONS_DATA_SAVE_ON_SUCCESS=none), so a
    streak can't be read from one snapshot — it's accumulated here. Per active ("Prod")
    workflow we find its most-recent error execution; the streak advances by one each time
    that id is NEW (a fresh failure since last cycle, so a single lingering failure isn't
    double-counted across cycles), and resets to 0 once the most-recent error ages past
    `window_s` (recovered / went idle) or no error is on record. `state` is a mutable
    {workflow_id: {"last_id", "streak"}} dict persisted across cycles. Returns
    {workflow_name: streak} for streak >= 1. Pure given (state, now) — unit-tested by
    driving cycles.
    """
    active = {
        w["id"]: (w.get("name") or w["id"])
        for w in workflows_json.get("data", [])
        if w.get("active")
    }
    latest = {}
    for ex in executions_json.get("data", []):
        wid = ex.get("workflowId")
        if wid not in active:
            continue
        ts = ex.get("stoppedAt") or ex.get("startedAt")
        if not ts:
            continue
        cur = latest.get(wid)
        if cur is None or ts > cur[1]:  # RFC3339 'Z' timestamps sort lexicographically
            latest[wid] = (ex.get("id"), ts)
    for wid in list(state):  # forget workflows that are no longer active
        if wid not in active:
            del state[wid]
    cutoff = now - timedelta(seconds=window_s)
    result = {}
    for wid, name in active.items():
        st = state.setdefault(wid, {"last_id": None, "streak": 0})
        info = latest.get(wid)
        if info is None:
            st["last_id"], st["streak"] = None, 0
            continue
        eid, ts = info
        dt = parse_rfc3339(ts)
        if (
            dt.tzinfo is None
        ):  # n8n emits UTC 'Z'; assume UTC if a naive ts slips through
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            st["last_id"], st["streak"] = None, 0
            continue
        if eid != st["last_id"]:
            st["streak"] += 1
            st["last_id"] = eid
        result[name] = st["streak"]
    return result


def n8n_verdict(streaks, consecutive_max, systemic_streak, systemic_max):
    """Pure: turn per-workflow failure streaks into an up/down verdict + message.

    Down if any single workflow has failed >= consecutive_max times in a row, OR if
    >= systemic_max workflows are each failing >= systemic_streak times — the n8n-wide catch
    that pages promptly as ONE alert (a broken n8n) instead of waiting for each workflow to
    reach consecutive_max, and instead of a per-workflow flood.
    """
    if not streaks:
        return True, "no active-workflow failures"
    ranked = sorted(streaks.items(), key=lambda nc: (-nc[1], nc[0]))
    systemic = [(n, c) for n, c in ranked if c >= systemic_streak]
    if len(systemic) >= systemic_max:
        names = ", ".join("%s (%d)" % (sanitize(n), c) for n, c in systemic[:5])
        return False, "n8n systemic: %d workflows failing repeatedly (%s)" % (
            len(systemic),
            names,
        )
    broken = [(n, c) for n, c in ranked if c >= consecutive_max]
    if broken:
        desc = ", ".join("%s (%d)" % (sanitize(n), c) for n, c in broken)
        return False, "n8n: %d active workflow(s) failed %d+ consecutive: %s" % (
            len(broken),
            consecutive_max,
            desc,
        )
    return True, "%d active workflow(s) failing (< %d consecutive)" % (
        len(ranked),
        consecutive_max,
    )


def gitops_alive(age_s, max_age_s):
    """Pure: is the deployer's last completed tick recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "deployer ran %.0fm ago" % (age_s / 60)
    return False, "deployer last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


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
    max_behind_s=GITOPS_BEHIND_MAX_S,
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


def sanitize(s, maxlen=120):
    """Neutralize adversary-controlled text before it enters a Discord-bound alert msg.

    Release titles, indexer names and n8n workflow names are attacker-influenced — a poisoned
    indexer/release is the very thing the arr-queue/prowlarr checks exist to catch. Kuma forwards
    the msg to Discord, which renders @mentions and markdown, so collapse newlines/whitespace,
    defuse '@' (which forms @everyone/@here/user pings) and backticks, and cap the length.
    """
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


def check_n8n():
    """Consecutive failures of active ("Prod") n8n workflows (streak accumulated across cycles).

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia). n8n
    doesn't save successful executions, so the per-workflow failure streak lives in the
    module-global _n8n_streaks and is advanced by n8n_update_streaks each cycle; n8n_verdict
    turns it into the page decision. Empty N8N_API_KEY -> disabled (stays up) so it never
    false-pages before the operator sets the key. An unreachable/erroring API raises -> the loop
    renders it down with the error, like check_targets_down (a dead API surfaces, not silent-green).
    """
    if not N8N_API_KEY:
        return True, "n8n monitoring disabled (no API key)"
    headers = {"X-N8N-API-KEY": N8N_API_KEY}
    workflows = _get_json(
        N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers
    )
    executions = _get_json(
        N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers
    )
    streaks = n8n_update_streaks(
        workflows,
        executions,
        _n8n_streaks,
        datetime.now(timezone.utc),
        parse_duration(N8N_FAIL_WINDOW),
    )
    return n8n_verdict(
        streaks, N8N_CONSECUTIVE_MAX, N8N_SYSTEMIC_STREAK, N8N_SYSTEMIC_MAX
    )


def queue_warnings(queue_json, app_name):
    """Pure: (app_name, title, reason) for each queue item needing an operator's eyes.

    Fed a sonarr/radarr /api/v3/queue payload. trackedDownloadStatus == "warning" is the
    2026-07-01 incident's signal — the *arr blocked the import itself but only flagged the
    queue item, so it kept seeding for a day with nothing paging. "error" is the harder
    sibling status (upstream enum: ok/warning/error) — at least as actionable, previously
    skipped. trackedDownloadState == "importBlocked" is the harder-blocked sibling state,
    "importFailed" its attempted-and-failed counterpart (both from the upstream
    TrackedDownloadState enum); "importPending" WITH statusMessages covers the case where
    the block reason shows up under the pending state instead. Plain "importPending" with
    no messages is the ordinary just-finished-download queue waiting its turn — not a
    problem, so it's left alone.
    """
    offenders = []
    for item in queue_json.get("records", []):
        status = item.get("trackedDownloadStatus")
        state = item.get("trackedDownloadState")
        messages = item.get("statusMessages") or []
        flagged = (
            status in ("warning", "error")
            or state in ("importBlocked", "importFailed")
            or (state == "importPending" and messages)
        )
        if not flagged:
            continue
        title = item.get("title") or "?"
        reasons = [m for sm in messages for m in sm.get("messages", [])]
        reason = "; ".join(reasons) or status or state or "warning"
        offenders.append((app_name, title, reason))
    return offenders


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
            SONARR_URL + "/api/v3/queue?includeUnknownSeriesItems=true&pageSize=250",
            SONARR_API_KEY,
        ),
        (
            "Radarr",
            # includeUnknownMovieItems is Radarr's spelling of Sonarr's
            # includeUnknownSeriesItems — both default FALSE, hiding exactly the unmapped/
            # poisoned-release queue items this check exists for (2026-07-01 incident class).
            RADARR_URL + "/api/v3/queue?includeUnknownMovieItems=true&pageSize=250",
            RADARR_API_KEY,
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


def indexers_down(status_json, name_by_id, now, min_down_min, ignore=None):
    """Pure: (name, minutes_down) for each Prowlarr indexer failing >= min_down_min minutes.

    Fed /api/v1/indexerstatus (a list of {indexerId, initialFailure, disabledTill, ...}) and an
    indexerId->name map from /api/v1/indexer. An indexer is listed in indexerstatus only while
    Prowlarr has it disabled due to failures; initialFailure is when the CURRENT failure run
    started, so (now - initialFailure) is the outage duration — a flap that recovers before the
    threshold drops out of the list and never qualifies. A null/absent/unparseable initialFailure
    is skipped (treated as just-started) rather than crashing the whole check. `ignore` is an
    iterable of indexer names (matched case-insensitively) that are never flagged — for
    chronically-flaky public trackers (see PROWLARR_INDEXER_IGNORE). Sorted worst-first so the
    longest outage leads the alert msg.
    """
    cutoff_s = min_down_min * 60
    ignored = {n.strip().lower() for n in (ignore or ()) if n.strip()}
    offenders = []
    for s in status_json or []:
        init = s.get("initialFailure")
        if not init:
            continue
        try:
            age_s = (now - parse_rfc3339(init)).total_seconds()
        except ValueError, TypeError:
            continue
        if age_s >= cutoff_s:
            iid = s.get("indexerId")
            name = name_by_id.get(iid) or "indexer %s" % iid
            if name.strip().lower() in ignored:
                continue
            offenders.append((name, age_s / 60.0))
    offenders.sort(key=lambda nm: -nm[1])
    return offenders


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
    if not PROWLARR_API_KEY:
        return True, "prowlarr indexer monitoring disabled (no API key)"
    headers = {"X-Api-Key": PROWLARR_API_KEY}
    status = _get_json(PROWLARR_URL + "/api/v1/indexerstatus", headers=headers)
    indexers = _get_json(PROWLARR_URL + "/api/v1/indexer", headers=headers)
    name_by_id = {i.get("id"): i.get("name") for i in indexers}
    offenders = indexers_down(
        status,
        name_by_id,
        datetime.now(timezone.utc),
        PROWLARR_INDEXER_MIN_DOWN_MIN,
        PROWLARR_INDEXER_IGNORE.split(","),
    )
    if offenders:
        desc = "; ".join("%s down %.0fm" % (sanitize(n), m) for n, m in offenders[:5])
        return False, "%d indexer(s) failing >=%gm: %s" % (
            len(offenders),
            PROWLARR_INDEXER_MIN_DOWN_MIN,
            desc,
        )
    return True, "all %d indexer(s) ok (none failing >=%gm)" % (
        len(name_by_id),
        PROWLARR_INDEXER_MIN_DOWN_MIN,
    )


def check_gitops_alive():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, GITOPS_MAX_AGE_S)


def _read_gitops_marker(name):
    try:
        with open(os.path.join(GITOPS_STATE_DIR, name)) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def check_gitops_status():
    return gitops_status(
        _read_gitops_marker("hold_sha"),
        _read_gitops_marker("diverged_sha"),
        _read_gitops_marker("behind_since"),
    )


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


def _scrutiny_status_desc(status):
    """Human-readable reason for a non-zero Scrutiny device_status (a bitwise enum)."""
    if not isinstance(status, int):
        return "device_status %s" % status
    reasons = []
    if status & 1:
        reasons.append("SMART self-assessment FAILED")
    if status & 2:
        reasons.append("Scrutiny attribute threshold breached")
    return ", ".join(reasons) or ("device_status %s" % status)


def scrutiny_health(summary, temp_max=0):
    """Pure: any non-archived device reporting a drive failure or over-temp? (ok, msg).

    `summary` is scrutiny's /api/summary data.summary dict. device_status is 0 when the drive
    passes both SMART's own self-assessment AND Scrutiny's attribute thresholds, non-zero on a
    failure — the actual drive-failure signal the freshness check (which only proves the collector
    still reports) can't see. A missing device_status is treated as unknown -> ok (don't false-page
    on an API that omits the field). temp_max > 0 adds a temperature ceiling (°C); 0 disables it.
    """
    failing, hot = [], []
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        name = dev.get("device_name") or wwn
        status = dev.get("device_status")
        if status not in (0, None):
            failing.append("%s (%s)" % (name, _scrutiny_status_desc(status)))
        if temp_max:
            temp = (entry.get("smart") or {}).get("temp")
            if temp is not None and temp > temp_max:
                hot.append("%s (%g°C > %g°C)" % (name, temp, temp_max))
    problems = failing + hot
    if problems:
        return False, "SMART health: " + ", ".join(problems)
    return True, "SMART health ok"


def check_scrutiny():
    data = _get_json(SCRUTINY_URL + "/api/summary")
    summary = (data.get("data") or {}).get("summary")
    fresh_ok, fresh_msg = scrutiny_freshness(summary, SCRUTINY_MAX_AGE_H)
    if not fresh_ok:
        return False, fresh_msg
    health_ok, health_msg = scrutiny_health(summary, SCRUTINY_TEMP_MAX)
    if not health_ok:
        return False, health_msg
    return True, "%s; %s" % (fresh_msg, health_msg)


def ups_health(charge_pct, runtime_s, replace_battery, charge_min_pct, runtime_min_s):
    """Pure: is the UPS battery healthy given charge (%), estimated runtime (s), and the replace-
    battery verdict (0/1)? (ok, msg).

    Any value may be None (that metric absent) — only present arms are judged, and the caller handles
    the all-absent / partial-absence cases. A low charge means an active deep discharge on battery; a
    low runtime means an aged battery whose full-charge runway has decayed OR a discharge nearing
    shutdown; replace_battery>0 is the UPS's OWN self-test verdict (NUT RB flag), which can trip while
    charge/runtime still read fine — the earliest replace-the-battery signal. Strict `<`, so a value
    exactly at the floor is still ok.
    """
    problems = []
    if charge_pct is not None and charge_pct < charge_min_pct:
        problems.append("battery %.0f%% (< %.0f%%)" % (charge_pct, charge_min_pct))
    if runtime_s is not None and runtime_s < runtime_min_s:
        problems.append(
            "runtime %.1fm (< %.1fm)" % (runtime_s / 60.0, runtime_min_s / 60.0)
        )
    if replace_battery is not None and replace_battery > 0.5:
        problems.append("replace-battery (UPS self-test / RB flag)")
    if problems:
        return False, "; ".join(problems)
    parts = []
    if charge_pct is not None:
        parts.append("battery %.0f%%" % charge_pct)
    if runtime_s is not None:
        parts.append("runtime %.1fm" % (runtime_s / 60.0))
    if replace_battery is not None:
        parts.append("self-test ok")
    return True, ", ".join(parts)


_ups_down_streak = 0


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
        a NUT outage can't reach the all-absent branch above. The nut container healthcheck owns
        NUT-server death, so defer rather than double-paging it with a misdirecting "entity renamed?".
    A PARTIAL absence that is NEITHER of those (a single numeric arm gone, or the replace arm gone
    while the numerics report) is a specific entity rename/removal — it pages (through the streak)
    rather than silently monitoring the survivor. UPS_CONSECUTIVE hysteresis (like check_ha_heartbeat)
    rides out a single-cycle runtime dip from a load spike or an HA-restart blip; only a sustained
    problem pages.
    """
    global _ups_down_streak
    configured = [
        (name, q)
        for name, q in (
            ("charge", UPS_CHARGE_QUERY),
            ("runtime", UPS_RUNTIME_QUERY),
            ("replace-battery", UPS_REPLACE_QUERY),
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
        ha_up = prom_scalar(UPS_HA_UP_QUERY) if UPS_HA_UP_QUERY else None
        if not (ha_up is not None and ha_up > 0.5 and "replace-battery" in values):
            _ups_down_streak = 0
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
        # the all-absent branch above. The nut container healthcheck owns NUT-server death, so defer
        # rather than double-paging it through the partial-absence path below with a misdirecting
        # "entity renamed?" msg. A single numeric arm gone (charge XOR runtime) is still a real rename.
        _ups_down_streak = 0
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
            UPS_CHARGE_MIN_PCT,
            UPS_RUNTIME_MIN_S,
        )
    if ok:
        _ups_down_streak = 0
        return True, msg
    _ups_down_streak, ok, msg = down_streak(
        _ups_down_streak, UPS_CONSECUTIVE, msg, "grace"
    )
    return ok, msg


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
        per_core,
        avail_mb,
        max(devices.values()),
    )


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


def _check_state_file(path, missing_msg, bad_msg, decide):
    """Read a JSON state file written by a host cron and hand (state, age_s) to `decide`.

    Shared IO half of the state-file monitors (pi-peers/disk-prune): every one reads the same
    {ts, ok, msg} shape, so only the path, the two failure messages, and the pure decision differ.
    Returns `decide(state, age_s)`, or (False, msg) when the file is missing/unparseable.
    """
    try:
        with open(path) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, missing_msg
    except ValueError, TypeError:
        return False, bad_msg
    return decide(state, age_s)


def pi_peers(state, age_s, max_age_s):
    """Pure: did the last wg-easy Pi-peer backup pull succeed, and recently? (ok, msg).

    Same state-file idiom as disk_prune. The pull is the only path that carries the Pi's
    un-rebuildable WireGuard peer keys into backup scope; because it never --deletes, a silently
    failing pull leaves stale-but-present files in place — so a FAILED or STALE pull is the signal
    that the offsite copy of those keys has quietly stopped refreshing.
    """
    if not state.get("ok"):
        return False, "last Pi-peer backup pull FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last successful Pi-peer pull %.1fd ago (max %.1fd)" % (
            age_s / 86400,
            max_age_s / 86400,
        )
    return True, "Pi-peer pull ok %.1fd ago: %s" % (age_s / 86400, state.get("msg", ""))


def check_pi_peers():
    return _check_state_file(
        PI_PEERS_STATE,
        "no Pi-peer backup state (pull never ran?)",
        "Pi-peer backup state unparseable",
        lambda state, age_s: pi_peers(state, age_s, PI_PEERS_MAX_AGE_S),
    )


def disk_prune(state, age_s, max_age_s):
    """Pure: did the last disk-autoprune run succeed, and recently? (ok, msg).

    Same state-file idiom as verify/pi_peers. ok=false means the last prune command errored; a
    disk still full of real data after a clean prune is Root Disk's alert, not this one.
    """
    if not state.get("ok"):
        return False, "last disk autoprune FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last disk autoprune %.1fh ago (max %.1fh)" % (
            age_s / 3600,
            max_age_s / 3600,
        )
    return True, "disk autoprune ok %.1fh ago: %s" % (
        age_s / 3600,
        state.get("msg", ""),
    )


def check_disk_prune():
    return _check_state_file(
        DISK_PRUNE_STATE,
        "no disk-autoprune state (never ran?)",
        "disk-autoprune state unparseable",
        lambda state, age_s: disk_prune(state, age_s, DISK_PRUNE_MAX_AGE_S),
    )


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
        return False, "stale — automations last ran %.0fs ago (> %gs)" % (
            age,
            max_age_s,
        )
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
        state = _get_json(
            HA_URL + "/api/states/" + HA_HEARTBEAT_ENTITY,
            headers={"Authorization": "Bearer " + HA_TOKEN},
        )
        ok, msg = ha_heartbeat_fresh(state, HA_HEARTBEAT_MAX_AGE_S)
    except (
        Exception
    ) as e:  # unreachable/auth -> route through the streak, don't page yet
        ok, msg = False, "HA API unreachable: %s" % e
    if ok:
        _ha_down_streak = 0
        return True, msg
    _ha_down_streak, ok, msg = down_streak(
        _ha_down_streak, HA_CONSECUTIVE, msg, "deploy/restart grace"
    )
    return ok, msg


def loki_count(selector, window):
    """Instant LogQL query: total log lines for `selector` over `window`. None if no series.

    Loki's instant-query endpoint evaluates a metric query — here
    sum(count_over_time(SELECTOR[WINDOW])) — and returns a vector with the same
    [ts, value] shape prom_scalar parses, so we read result[0].value[1].
    """
    query = "sum(count_over_time(%s[%s]))" % (selector, window)
    result = _instant_query(LOKI_URL, "/loki/api/v1/query", query, "loki")
    if not result:
        return None
    return float(result[0]["value"][1])


def loki_ingestion_fresh(count, window):
    """Decide log-pipeline freshness from the line count over `window` (None = no series)."""
    if not count:  # None or 0 — nothing shipped: promtail dead, positions corrupt, etc.
        return (
            False,
            "no log lines ingested in %s — promtail/Loki pipeline silent" % window,
        )
    return True, "%d log lines in %s" % (int(count), window)


def check_loki_ingestion():
    # Two arms, down if EITHER pipeline is silent: the file-tail union (arm 1) catches a
    # file-tail break (all of authlog/syslog/traefik going silent — a total promtail death or
    # a static_configs/bind regression) over a tolerant window; the container-stream arm
    # (arm 2) catches a docker_sd-specific break the file-tail selector excludes (see
    # LOKI_DOCKER_STREAM). The docker stream dwarfs the file-tail streams, so arm 1 must NOT
    # include it (else a healthy docker stream masks a dead file-tail pipeline) — hence the
    # separate selector + wider window (LOKI_FILETAIL_WINDOW).
    ok_all, msg_all = loki_ingestion_fresh(
        loki_count(LOKI_STREAM, LOKI_FILETAIL_WINDOW), LOKI_FILETAIL_WINDOW
    )
    if not ok_all:
        return False, "file-tail streams silent — " + msg_all
    ok_docker, msg_docker = loki_ingestion_fresh(
        loki_count(LOKI_DOCKER_STREAM, LOKI_WINDOW), LOKI_WINDOW
    )
    if not ok_docker:
        return False, "container log stream silent — " + msg_docker
    return True, "%s (+ container stream)" % msg_all


def promtail_dropped(count, window, threshold):
    """Pure: did promtail drop more than `threshold` entries over `window`? (ok, msg).

    `count` = sum(increase(promtail_dropped_entries_total[window])) over ALL drop reasons
    (ingester_error / rate_limited / stream_limited / line_too_long), None when the counter has no
    series (reads as 0). Above the threshold means Loki was rejecting entries and promtail gave up on
    them — partial log loss the total-silence Loki Log Ingestion check can't see.
    """
    n = count or 0.0
    if n > threshold:
        return False, (
            "promtail dropped %.0f log entries in %s (> %.0f) — partial log loss"
            % (n, window, threshold)
        )
    return True, "promtail drops ok (%.0f in %s)" % (n, window)


def check_promtail_dropped():
    """Prometheus-based promtail partial-loss watchdog (see promtail_dropped). Prom-dependent."""
    count = prom_scalar(
        "sum(increase(%s[%s]))" % (PROMTAIL_DROPPED_SELECTOR, PROMTAIL_DROPPED_WINDOW)
    )
    return promtail_dropped(count, PROMTAIL_DROPPED_WINDOW, PROMTAIL_DROPPED_MAX)


def loki_reachable():
    """Is Loki itself reachable and answering queries? (the LOKI_DEPENDENT gate).

    Hits the labels endpoint — a fixed, ingestion-independent query that returns status=success
    whenever Loki is up — so 'Loki is down' (one root cause, one page: Loki Reachable) is separated
    from 'Loki is up but promtail stopped shipping' (Loki Log Ingestion, which still evaluates
    whenever Loki is reachable). Raising -> _evaluate renders the Loki Reachable monitor down.
    """
    data = _get_json(LOKI_URL + "/loki/api/v1/labels")
    if data.get("status") != "success":
        raise RuntimeError("loki labels status=%s" % data.get("status"))
    return True


def check_loki_reachable():
    loki_reachable()
    return True, "Loki reachable"


_b2_probe = {"ts": 0.0, "ok": True, "msg": "not yet probed"}


def b2_authorize():
    """Authenticate against B2. (ok, msg) — the msg carries B2's own error text on failure.

    Basic auth with the key id + application key is the whole protocol for b2_authorize_account.
    _get_json re-raises HTTPError with the response body appended, so a cap breach arrives here as
    "HTTP Error 403: ... transaction_cap_exceeded ..." and that string is what reaches Kuma and
    Discord — the named cause G3 asked for.
    """
    token = base64.b64encode(
        ("%s:%s" % (B2_PROBE_KEY_ID, B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    data = _get_json(B2_PROBE_URL, headers={"Authorization": "Basic %s" % token})
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

    Empty credentials -> disabled (stays up), like check_n8n's empty API key. BOTH outcomes are
    cached for B2_PROBE_INTERVAL_S: unlike email_backstop, a failure must not re-probe every cycle,
    because the failure being detected is a transaction cap and retrying would spend more of it.
    The cached verdict is returned (and pushed) every cycle regardless, so the push monitor's
    heartbeat stays alive and the dead-bridge watchdog isn't tripped.

    Module-global cache, reset on container restart, like the streak counters.
    """
    if not B2_PROBE_KEY_ID or not B2_PROBE_APPLICATION_KEY:
        return True, "B2 reachability check disabled (no credentials)"
    now = now if now is not None else time.time()
    if now - _b2_probe["ts"] < B2_PROBE_INTERVAL_S:
        return _b2_probe["ok"], "%s (checked %.0fm ago)" % (
            _b2_probe["msg"],
            (now - _b2_probe["ts"]) / 60,
        )
    try:
        ok, msg = b2_authorize()
    except Exception as e:
        ok, msg = False, "B2 unreachable: %s" % e
    _b2_probe["ts"] = now
    _b2_probe["ok"] = ok
    _b2_probe["msg"] = msg
    return ok, msg


def check_b2_reachable():
    return b2_reachable()


def k8s_workloads_verdict(
    total,
    offenders,
    min_workloads,
    restart_offenders=(),
    ds_total=None,
    ds_offenders=(),
    min_daemonsets=None,
):
    """Pure: (ok, msg) from the deployment-series COUNT and the unavailable-replica offenders.

    The count argument is what makes this fail closed. `unavailable > 0` returning nothing is
    ambiguous — it means either "every workload is healthy" or "there are no series at all" —
    and only the second is a fault. Reading the first interpretation onto both is how a monitor
    goes green while blind, so the count is checked BEFORE the offender list is trusted.

    restart_offenders is the crash-loop arm (2026-08-13): a CrashLoopBackOff pod passes its
    readiness probe for a brief window each backoff cycle, so replica availability AND a 60s
    HTTP tile both mostly read healthy — homepage crash-looped 31 times overnight with this
    check green. A restart counter that climbed past the threshold is down regardless of what
    readiness says right now.

    ds_total/ds_offenders/min_daemonsets are the DaemonSet arm (2026-08-13): a DaemonSet has no
    Deployment-arm equivalent, so an absent or unschedulable DS pod (a node NotReady, a
    node-selector mismatch, a node lacking a required resource) was invisible. Same fail-closed
    shape as the deployment arm; min_daemonsets left None means the caller didn't supply
    DaemonSet data (existing callers/tests), so this arm is skipped rather than treated as zero
    DaemonSets.
    """
    if total is None:
        return False, (
            "kube_deployment_status_replicas_unavailable is absent from the cluster Prometheus "
            "— kube-state-metrics is not being scraped, so workload health is UNKNOWN, not OK"
        )
    if total < min_workloads:
        return False, (
            "only %d deployment series in the cluster Prometheus, below the floor of %d — "
            "kube-state-metrics is partially loaded, so workload health is UNKNOWN, not OK"
            % (int(total), min_workloads)
        )
    if min_daemonsets is not None:
        if ds_total is None:
            return False, (
                "kube_daemonset_status_number_unavailable is absent from the cluster Prometheus "
                "— kube-state-metrics is not being scraped, so daemonset health is UNKNOWN, not OK"
            )
        if ds_total < min_daemonsets:
            return False, (
                "only %d daemonset series in the cluster Prometheus, below the floor of %d — "
                "kube-state-metrics is partially loaded, so daemonset health is UNKNOWN, not OK"
                % (int(ds_total), min_daemonsets)
            )
    if offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("deployment", "?"), int(value))
            for labels, value in sorted(
                offenders, key=lambda o: o[0].get("deployment", "")
            )
        )
        return False, "k8s workloads with unavailable replicas: %s" % named
    if ds_offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("daemonset", "?"), int(value))
            for labels, value in sorted(
                ds_offenders, key=lambda o: o[0].get("daemonset", "")
            )
        )
        return False, "k8s daemonsets with unavailable pods: %s" % named
    if restart_offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("pod", "?"), int(value))
            for labels, value in sorted(
                restart_offenders, key=lambda o: o[0].get("pod", "")
            )
        )
        return False, "k8s pods crash-looping (restarts in window): %s" % named
    return True, "%d k8s workloads healthy" % int(total)


def check_k8s_workloads():
    """Deployment readiness for every workload in the k3s cluster.

    Gated by check_cluster_prometheus rather than the ordinary Prometheus gate: this is the one
    check reading the CLUSTER Prometheus, so the `prom_ok` gate is not watching its source. See
    CLUSTER_DEPENDENT.
    """
    if not CLUSTER_PROM_URL:
        return True, "k8s workload check disabled (no CLUSTER_PROMETHEUS_URL)"
    total = prom_scalar(
        "count(kube_deployment_status_replicas_unavailable)",
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    offenders = prom_vector(
        "kube_deployment_status_replicas_unavailable > 0",
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    restart_offenders = prom_vector(
        "increase(kube_pod_container_status_restarts_total[%s]) > %d"
        % (K8S_RESTART_WINDOW, K8S_RESTART_MAX),
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_total = prom_scalar(
        "count(kube_daemonset_status_number_unavailable)",
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_offenders = prom_vector(
        "kube_daemonset_status_number_unavailable > 0",
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    return k8s_workloads_verdict(
        total,
        offenders,
        K8S_MIN_WORKLOADS,
        restart_offenders,
        ds_total,
        ds_offenders,
        K8S_MIN_DAEMONSETS,
    )


def check_cluster_targets():
    """Scrape targets of the CLUSTER's own Prometheus (the other half of Scrape Targets).

    B5 pinned check_targets_down to origin="daniel-server" so it kept meaning exactly what it
    always meant. The cost of that, unpaid until now, is that the cluster's own five targets were
    watched by nothing: cluster_prometheus probes only reachability, and k8s_workloads reads
    deployment replicas rather than scrape health. kube-state-metrics failing is covered by
    accident (its series vanish and the workload check fails closed on the floor), but
    otel-collector and otel-collector-internal going down was silent — and those two carry the
    only copy of Claude Code's session/token/cost telemetry.

    `origin=""` selects series where the label is ABSENT, which is exactly the cluster-native set:
    daniel-server's remote-written series all carry it. The same floor logic as its sibling, so an
    emptied `up` reads as UNKNOWN rather than as nothing being wrong.
    """
    if not CLUSTER_PROM_URL:
        return True, "cluster target check disabled (no CLUSTER_PROMETHEUS_URL)"
    vec = prom_vector(
        'up{origin=""}', base=CLUSTER_PROM_URL, source="cluster prometheus"
    )
    return targets_verdict(vec, CLUSTER_TARGETS_MIN)


def check_cluster_prometheus():
    """Reachability gate for the cluster Prometheus — the peer of check_prometheus.

    Kept separate from the Docker Prometheus gate on purpose. They are different instances on
    different hosts reached by different paths, so one `prom_ok` cannot describe both: a gate
    that is not watching a check's actual source is worse than no gate, because it reports
    confidence it does not have.
    """
    if not CLUSTER_PROM_URL:
        return True, "cluster Prometheus check disabled (no CLUSTER_PROMETHEUS_URL)"
    value = prom_scalar("vector(1)", base=CLUSTER_PROM_URL, source="cluster prometheus")
    if value is None:
        return False, "cluster Prometheus returned no result for vector(1)"
    return True, "cluster Prometheus reachable"


def discord_webhook_ok(status_code, name=None):
    """Pure: does a GET on a Discord webhook return 200 (still valid)? (ok, msg).

    Discord answers a webhook GET with its JSON metadata (id/name) and HTTP 200 while the
    webhook exists, and 404 once it's been rotated/revoked/deleted — so a non-200 means the
    alert POSTs won't deliver. (A GET never posts a message, so this can't spam.)
    """
    if status_code == 200:
        return True, "Discord webhook valid%s" % (" (%s)" % name if name else "")
    return (
        False,
        "Discord webhook returned HTTP %s — alerts won't deliver" % status_code,
    )


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
            ("Kuma", DISCORD_WEBHOOK_URL),
            ("CrowdSec", DISCORD_CROWDSEC_WEBHOOK_URL),
            ("GitOps/Renovate", DISCORD_GITOPS_WEBHOOK_URL),
            ("Arr", DISCORD_ARR_WEBHOOK_URL),
            ("Healthchecks", DISCORD_HEALTHCHECKS_WEBHOOK_URL),
        )
        if url
    ]


def _smtp_login_ok():
    """Connect to the SMTP server over implicit TLS and AUTH with the notify creds. (ok, msg).

    A revoked/expired Gmail app-password fails at login; a broken SMTP endpoint fails at connect. NOOP
    then QUIT — never sends a message. Raises are caught by the caller and ridden through the streak.
    """
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT, context=ctx) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.noop()
    return True, "SMTP login ok (%s)" % SMTP_USER


_email_probe = {"ts": 0.0, "ok": True, "msg": "not yet probed"}


def email_backstop(now=None):
    """Throttled deliverability probe for the alert-email 2nd channel. (ok, msg).

    Empty SMTP_PASSWORD -> disabled (stays up). A SUCCESS is cached for EMAIL_PROBE_INTERVAL_S (so
    Gmail doesn't see an AUTH every cycle); a FAILURE isn't cached, so it re-probes every cycle until
    it recovers — and check_discord's DISCORD_CONSECUTIVE streak rides out a transient blip before
    paging. Module-global cache, reset on container restart, like the streak counters — no persistent
    state needed.
    """
    if not SMTP_PASSWORD:
        return True, "email backstop disabled (no SMTP password)"
    now = now if now is not None else time.time()
    if _email_probe["ok"] and now - _email_probe["ts"] < EMAIL_PROBE_INTERVAL_S:
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


_discord_down_streak = 0


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
    global _discord_down_streak
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
        _discord_down_streak = 0
        return True, "delivery channels valid (%s)" % ", ".join(valid)
    _discord_down_streak, ok, msg = down_streak(
        _discord_down_streak, DISCORD_CONSECUTIVE, msg, "transient grace"
    )
    return ok, msg


CHECKS = [
    ("disk", _env("KUMA_PUSH_DISK", ""), check_disk),
    ("cert", _env("KUMA_PUSH_CERT", ""), check_cert),
    ("memory", _env("KUMA_PUSH_MEM", ""), check_mem),
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
    (
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    ("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    ("pi_peers", _env("KUMA_PUSH_PI_PEERS", ""), check_pi_peers),
    ("disk_prune", _env("KUMA_PUSH_DISK_PRUNE", ""), check_disk_prune),
    ("scrutiny", _env("KUMA_PUSH_SCRUTINY", ""), check_scrutiny),
    ("ups", _env("KUMA_PUSH_UPS", ""), check_ups),
    ("pi_pressure", _env("KUMA_PUSH_PI", ""), check_pi_pressure),
    ("ha_heartbeat", _env("KUMA_PUSH_HA", ""), check_ha_heartbeat),
    ("renovate_alive", _env("KUMA_PUSH_RENOVATE_ALIVE", ""), check_renovate_alive),
    ("loki_ingestion", _env("KUMA_PUSH_LOKI", ""), check_loki_ingestion),
    (
        "promtail_dropped",
        _env("KUMA_PUSH_PROMTAIL_DROPPED", ""),
        check_promtail_dropped,
    ),
    ("discord", _env("KUMA_PUSH_DISCORD", ""), check_discord),
    ("k8s_workloads", _env("KUMA_PUSH_K8S_WORKLOADS", ""), check_k8s_workloads),
    ("cluster_targets", _env("KUMA_PUSH_CLUSTER_TARGETS", ""), check_cluster_targets),
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
        "promtail_dropped",  # increase(promtail_dropped_entries_total) instant query
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
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory"}),
    "cadvisor": frozenset({"restarts", "oom", "cpu"}),
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
# docs/k3s-migration/backup-consolidation-longhorn.md) — leaving this empty. b2_reachable itself
# stays: Longhorn still needs B2. Kept as infrastructure for any future check that reads B2-backed
# state via a cron/state-file rather than querying B2 live.
B2_DEPENDENT = frozenset()

# Checks that read the CLUSTER Prometheus (daniel-box) rather than the Docker one. Its own gate,
# not an arm of PROM_DEPENDENT, because they are two instances on two hosts reached by two paths —
# the Docker Prometheus being up says nothing about whether the cluster one is, and a gate that
# is not watching a check's real source reports confidence it does not have.
#
# The division of labour with check_k8s_workloads' own fail-closed logic is deliberate and the two
# halves are not interchangeable. THIS gate covers "the cluster Prometheus is unreachable", which
# is a root cause that would otherwise page as a workload fault. The check's series-count floor
# covers "the cluster Prometheus is reachable but kube-state-metrics is not being scraped" — which
# this gate structurally cannot see, because the Prometheus answering `vector(1)` is perfectly
# healthy. Suppression is right for the first and would be dangerous for the second: it would turn
# a blind monitor green.
CLUSTER_DEPENDENT = frozenset({"k8s_workloads", "cluster_targets"})

# Reach-out checks that poll a live app dependency (n8n/sonarr/radarr/prowlarr/scrutiny/the Pi
# glances) with NO reachability gate above them and NO per-check hysteresis of their own — unlike
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
    {"n8n", "arr_queue", "pi_pressure", "prowlarr_indexers", "scrutiny"}
)

_grace_streaks = {}

# Which checks THIS instance runs. The Phase F drain splits the bridge in two deployments of
# this same file: the cluster twin owns every metric/API check, and the Docker remnant keeps
# only the checks that read daniel-server host state files (gitops_alive, gitops_status,
# pi_peers, disk_prune, renovate_alive) — split by env instead of a fork, so the twins can't
# drift. CHECKS_ONLY (comma-separated names) enables exactly that set; CHECKS_SKIP drops
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


def _evaluate(name, fn):
    """Run one check; convert an unreachable source/metric into a descriptive `down` instead
    of letting it kill the loop. Returns (ok, msg)."""
    try:
        return fn()
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return False, "%s check error: %s" % (name, e)


def run_once():
    # Prometheus reachability is evaluated FIRST and gates the prom-dependent checks: a single
    # Prometheus outage would otherwise page all of them at once (one root cause, an alert storm).
    # When it's down they're suppressed (pushed `up` with a skip msg, keeping each push monitor's
    # heartbeat alive) so only the Prometheus monitor pages; a real per-metric problem still alerts
    # whenever Prometheus is up.
    prom_ok, prom_msg = True, "disabled by check filter"
    if check_enabled("prometheus"):
        prom_ok, prom_msg = _evaluate("prometheus", check_prometheus)
        log("OK  " if prom_ok else "DOWN", "prometheus", "-", prom_msg)
        push(_env("KUMA_PUSH_PROMETHEUS", ""), prom_ok, prom_msg)

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
            log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (LOKI_DEPENDENT).
    loki_ok, loki_msg = True, "disabled by check filter"
    if check_enabled("loki_reachable"):
        loki_ok, loki_msg = _evaluate("loki_reachable", check_loki_reachable)
        log("OK  " if loki_ok else "DOWN", "loki_reachable", "-", loki_msg)
        push(_env("KUMA_PUSH_LOKI_REACHABLE", ""), loki_ok, loki_msg)

    # B2-reachability gate (peer of the two above): B2 caps TRANSACTIONS separately from storage
    # bytes, and the kopia-era state-file checks this used to gate all reported their last
    # successful cron run rather than current B2 health — the 2026-08-02 transaction-cap incident.
    # Those checks are gone (backup moved to Longhorn), but b2_reachable stays: Longhorn still
    # needs B2. The probe is throttled inside b2_reachable (it must not spend the transaction
    # budget it is watching), but the cached verdict is pushed every cycle so this monitor's own
    # heartbeat stays alive.
    b2_ok, b2_msg = True, "disabled by check filter"
    if check_enabled("b2_reachable"):
        b2_ok, b2_msg = _evaluate("b2_reachable", check_b2_reachable)
        log("OK  " if b2_ok else "DOWN", "b2_reachable", "-", b2_msg)
        push(_env("KUMA_PUSH_B2_REACHABLE", ""), b2_ok, b2_msg)

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
    cluster_ok, cluster_msg = True, "disabled by check filter"
    if check_enabled("cluster_prometheus"):
        # The same-instance reuse only holds when the prometheus gate actually probed.
        if (
            CLUSTER_PROM_URL
            and CLUSTER_PROM_URL == PROM_URL
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
        log("OK  " if cluster_ok else "DOWN", "cluster_prometheus", "-", cluster_msg)
        push(_env("KUMA_PUSH_CLUSTER_PROMETHEUS", ""), cluster_ok, cluster_msg)

    for name, token, fn in CHECKS:
        if not check_enabled(name):
            continue
        if not prom_ok and name in PROM_DEPENDENT:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            log("SKIP", name, "-", msg)
        elif not loki_ok and name in LOKI_DEPENDENT:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
            log("SKIP", name, "-", msg)
        elif not b2_ok and name in B2_DEPENDENT:
            ok, msg = True, "skipped — B2 unreachable (see B2 Reachable monitor)"
            log("SKIP", name, "-", msg)
        elif not cluster_ok and name in CLUSTER_DEPENDENT:
            ok, msg = (
                True,
                "skipped — cluster Prometheus unreachable (see Cluster Prometheus monitor)",
            )
            log("SKIP", name, "-", msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see Scrape Targets)"
            log("SKIP", name, "-", msg)
        else:
            ok, msg = _evaluate(name, fn)
            if name in STARTUP_GRACE:
                ok, msg = apply_startup_grace(
                    name, ok, msg, GRACE_CYCLES, _grace_streaks
                )
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
    problems = validate_check_filter(CHECKS_ONLY, CHECKS_SKIP, CHECKS)
    if problems:
        for p in problems:
            log("FATAL: bad CHECKS_ONLY/CHECKS_SKIP:", p)
        sys.exit(2)
    enabled = [name for name, _, _ in CHECKS if check_enabled(name)]
    log(
        "monitor-bridge starting (interval=%ss, once=%s, checks=%d/%d)"
        % (INTERVAL, once, len(enabled), len(CHECKS))
    )
    while True:
        run_once()
        touch_heartbeat()
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
