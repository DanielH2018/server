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

# Pure helpers, split out of this file. `_env`/`sanitize` are imported by name because
# nothing patches them; anything the test suite patches must stay defined in THIS module,
# or `monkeypatch.setattr(check, ...)` rebinds a name no code reads — bridge_parsing.py's
# header carries the full rule. `log`/`touch_heartbeat` ARE patched (indirectly, via
# HEARTBEAT_FILE for the latter), so this file reaches them qualified as
# `bridge_common.log`/`bridge_common.touch_heartbeat` rather than importing them by name —
# enforced by ansible/tests/test_bridge_patch_boundary.py.
import bridge_common
from bridge_common import _env, sanitize
from bridge_parsing import (
    FETCH_BODY_MAX,
    describe_fetch_failure,
    endpoint_label,
    parse_duration,
)
from verdicts_cluster import (
    extended_resource_verdict,
    k8s_workloads_verdict,
    ksm_resource_label,
    log_error_verdict,
    targets_verdict,
)
from verdicts_host import (
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
# Origins that check_disk/check_mem must NOT scan, as a regex alternation. See host_metric_sel:
# daniel-pi runs node-exporter like the other hosts, but check_pi_pressure owns its disk and
# memory with thresholds sized for a 456 MB box.
HOST_METRIC_ORIGIN_EXCLUDE = _env("HOST_METRIC_ORIGIN_EXCLUDE", "daniel-pi")
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
# Since the Docker uninstall (2026-08-14) this reads daniel-box's own deployer state —
# the pod is pinned to that node and hostPath-mounts /var/lib/gitops-deploy. One
# deployer remains in the fleet (the Pi runs has_gitops: false), so one watcher.
GITOPS_STATE_DIR = _env("GITOPS_STATE_DIR", "/gitops-state")
GITOPS_MAX_AGE_S = float(_env("GITOPS_MAX_AGE_MIN", "90")) * 60
# How long the host may sit behind origin before GitOps Status pages. Generous on purpose: the
# deployer ticks every 30 min, and the dirty-tree path (operator mid-edit) is behind by design for
# as long as the edit lasts. 6 h pages a genuinely-stuck host well inside a day while never firing
# on a normal push or a long editing session.
GITOPS_BEHIND_MAX_S = float(_env("GITOPS_BEHIND_MAX_MIN", "360")) * 60
# pi_peers + renovate_alive checks REMOVED at the 2026-08-14 host flips: the peer pull
# is the k8s/pi-peer-backup CronJob and the notifier's ExecStartPost pushes its own
# beat — both push their Kuma monitors directly, so no state file and no check remain.

# Every-5-min CrowdSec home-IP allowlist updater (traefik role's crowdsec-update-home-allowlist.sh):
# keeps the operator's current home public IP in CrowdSec's `home-ips` allowlist so the public path
# from home doesn't trip the WAF. It writes {"ts": epoch, "ok": bool, "msg": str} on EVERY run (incl.
# the common IP-unchanged fast path). It was the last self-`logger`ing cron with no watchdog — a silent
# failure (ipify unreachable, cscli error) just meant occasional 403s on the next IP rotation, invisible
# until noticed. We alert on a FAILED run or staleness (cron broken / never ran). 30 min = 6 missed
# 5-min runs; the fast-path heartbeat keeps a healthy no-op green.


# disk_prune check REMOVED at the Docker uninstall (2026-08-14): the hourly
# docker/builder prune it watched existed for the Docker daemon's disk, and both retired
# together. containerd's own image GC owns that concern on the k3s tier; a genuinely
# full disk is still the Root Disk check's alert.

# B2 REACHABILITY — the gap the 2026-08-02 transaction-cap incident exposed
# (docs/b2-transaction-cap-monitoring-gaps.md). B2 caps TRANSACTIONS separately from storage
# bytes; the kopia-era state-file checks this used to gate reported their last successful cron
# run rather than current B2 health, so all of them read green — "B2 6.05/10GB billable (60% of
# plan)" among them — for nine and a half hours while B2 refused every request. Worse than absent
# — an operator triaging the one true alert was told by these that B2 was fine. Those checks were
# removed 2026-08-10 (kopia is retired, backup moved to Longhorn — see
# docs/archive/k3s-migration/backup-consolidation-longhorn.md), but this probe stays: Longhorn still needs
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

# B2 free-tier STORAGE headroom — the other half of the B2 budget, billed separately from the
# transaction cap b2_reachable watches. kopia reported this as `kopia_b2_billable_bytes`; that
# metric retired with kopia on 2026-08-10 and nothing replaced it, so the storage half went
# unwatched at exactly the point Longhorn became its only client. Two Grafana panels kept querying
# the dead gauge and rendered blank while looking authoritative.
#
# Listing versions is the only way to see the real number: hidden and unfinished versions bill as
# stored bytes and do NOT appear in a plain object listing, which is how the cap filled unnoticed
# before (hidden kopia bytes wedged retention deletes, 2026-08-13).
B2_STORAGE_CAP_BYTES = float(_env("B2_STORAGE_CAP_BYTES", str(10 * 1000**3)))
B2_STORAGE_MAX_PCT = float(_env("B2_STORAGE_MAX_PCT", "80"))
# Daily rather than B2_PROBE_INTERVAL_S: a full listing costs one Class C call per 1000 versions,
# and a check that guards a budget must not be a meaningful part of the spend. At ~5k versions
# that is ~5 calls/day against a 2500/day Class C allowance.
B2_STORAGE_INTERVAL_S = float(_env("B2_STORAGE_INTERVAL_S", "86400"))
# Stop paging rather than walk forever if the bucket is far larger than expected. Hitting this is
# itself reported, because a truncated sum under-reports usage — the direction that reads as
# headroom we do not have.
B2_STORAGE_MAX_PAGES = int(_env("B2_STORAGE_MAX_PAGES", "50"))

# Cloudflare R2 free-tier headroom, via the GraphQL Analytics API. Cloudflare offers NO spending
# cap or usage limit on R2 — on any plan — so the only boundary is one we watch and act on. The
# Usage-Based Billing notification that would do this natively needs a Pro plan; this account is on
# Free. Overage is cheap ($0.015/GB-month, $4.50/M Class A, $0.36/M Class B, egress free), so this
# is a headroom guard, not a bill-shock alarm: it exists to notice a runaway client (the shape of
# the 2026-08-13 Longhorn retry storm, which burned ~2.5k B2 Class B/day against a cap) while the
# fix is still a config edit.
#
# CF_ANALYTICS_TOKEN is file-mounted like HA_TOKEN and the B2 key, for the same reason (H2): an
# account-scoped credential must not sit inline in the container Env. Its ONLY permission is
# Account Analytics: Read — it cannot touch bucket data, which is why the check reads usage and
# pages rather than revoking anything itself.
CF_GRAPHQL_URL = _env("CF_GRAPHQL_URL", "https://api.cloudflare.com/client/v4/graphql")
CF_ACCOUNT_ID = _env("CF_ACCOUNT_ID", "")
CF_ANALYTICS_TOKEN = _env_file("CF_ANALYTICS_TOKEN")
R2_BUCKET = _env("R2_BUCKET", "")
R2_STORAGE_MAX_GB = float(_env("R2_STORAGE_MAX_GB", "10"))
R2_CLASS_A_MAX = float(_env("R2_CLASS_A_MAX", "1000000"))
R2_CLASS_B_MAX = float(_env("R2_CLASS_B_MAX", "10000000"))
R2_USAGE_MAX_PCT = float(_env("R2_USAGE_MAX_PCT", "80"))
# Outstanding incomplete multipart uploads. These bill as stored bytes but do NOT appear in a
# normal object listing, so they are the quiet way a 10 GB budget fills. The durable fix is a
# bucket lifecycle rule (AbortIncompleteMultipartUpload) — a one-time operator step, see this
# role's CLAUDE.md — and this arm is the backstop that notices when it is absent or not working.
R2_UPLOADS_MAX = float(_env("R2_UPLOADS_MAX", "25"))
# SUCCESSES are cached for this long; a failure re-probes next cycle. The opposite of
# B2_PROBE_INTERVAL_S's cache-both, and deliberately so: the fault B2 detects is a spend cap that
# retrying makes worse, whereas GraphQL analytics calls are free and count against no R2 budget.
# So this follows EMAIL_PROBE_INTERVAL_S's cache-successes-only idiom, and rides out a transient
# Cloudflare blip through STARTUP_GRACE rather than through a stale cached failure.
R2_PROBE_INTERVAL_S = float(_env("R2_PROBE_INTERVAL_S", "1800"))

# R2 bills operations in two classes, and the GraphQL API reports raw actionType names without
# saying which class each falls in — so the mapping has to live here. From the R2 pricing page.
# DeleteObject, DeleteBucket and AbortMultipartUpload are free and counted in neither.
R2_CLASS_A_ACTIONS = frozenset(
    {
        "ListBuckets",
        "PutBucket",
        "ListObjects",
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
        "CreateMultipartUpload",
        "LifecycleStorageTierTransition",
        "ListMultipartUploads",
        "UploadPart",
        "UploadPartCopy",
        "ListParts",
        "PutBucketEncryption",
        "PutBucketCors",
        "PutBucketLifecycleConfiguration",
    }
)
R2_CLASS_B_ACTIONS = frozenset(
    {
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    }
)
R2_FREE_ACTIONS = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})

# k3s workload health, via the CLUSTER's Prometheus — a SECOND Prometheus, not the one PROM_URL
# points at. Slice 3 D8 (docs/archive/k3s-migration/slice-3-monitoring-plane.md). Seven k8s workloads ran
# from slice 2 with no monitor of any kind, n8n-runners among them, because none of them is
# probeable from here: three expose only a ClusterIP, four expose no Service at all, and none has
# an ingress route. Their health is a Kubernetes API property, so kube-state-metrics is the only
# thing that can express it as something this bridge can query.
#
# Reached over the in-cluster Service DNS name (see templates/env-secret.yaml.j2) — the bridge has
# run in-cluster since 2026-08-14, so there is no VIP, no Traefik and no gate in this probe's path.
# It went over the ingress while the bridge was on daniel-server; that route is now
# prometheus.local.<domain>, the `-k8s` suffix having retired on 2026-08-15.
# Empty = disabled (stays up), like N8N_API_KEY.
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
# (cert/restarts/oom/cpu/targets/ups/promtail_dropped). Disk and memory are the two exceptions:
# they are HOST checks, and pinning them to one origin would leave the other host's root disk and
# memory unwatched — so they group `by (origin)` and report the worst, covering both. Since E2 the cluster edge also
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
# targets_verdict.
# CORRECTED 2026-08-24: this said "exactly two origin="daniel-server" jobs: node, cadvisor". It
# is ONE — `node`. Only the node job is relabelled with `origin`
# (claude-otel/templates/prometheus.yaml.j2:159); the cadvisor job never was, which is the whole
# mechanism behind the blind restarts/oom/cpu checks fixed the same day. A reviewer checking that
# finding against this comment would have cleared it, so the stale half is corrected here rather
# than left to be re-derived.
# The deployed TARGETS_MIN is 1 (env-secret.yaml.j2), which matches that single job and still
# fails closed: targets_verdict tests `len(vec) < min_targets`, so an empty vector is 0 < 1 and
# reports UNKNOWN. A floor of 1 cannot detect a PARTIAL shortfall, but with one expected series
# there is no partial case to detect. The code default of 2 is kept only as the fail-safe for a
# host whose env omits the key entirely.
TARGETS_MIN = int(_env("TARGETS_MIN", "2"))
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


def cadvisor_sel(*matchers):
    """A `{...}` block for cAdvisor series, which carry NO origin label — so no origin pin.

    DECIDED: cAdvisor metrics must NOT go through origin_sel(). `origin` is applied by exactly
    one relabel rule, on the `node` job (claude-otel/templates/prometheus.yaml.j2:159); the
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
    if HOST_METRIC_ORIGIN_EXCLUDE:
        parts.append('origin!~"%s"' % HOST_METRIC_ORIGIN_EXCLUDE)
    return "{%s}" % ", ".join(parts) if parts else ""


def _origin_name(labels):
    """The host a per-origin series belongs to, for naming an offender in an alert message.

    The Docker Prometheus has no `origin` label at all (external_labels are applied on
    remote-write, never to local queries), so an empty one means "the only host there is".
    """
    return labels.get("origin") or "host"


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
# Extended resources that must stay ADVERTISED by at least one node. The DaemonSet arm above
# watches whether the plugin's POD is running; this watches whether the thing the pod exists to
# provide is still there. dri-device-plugin has no probe — and a container with no readinessProbe
# is Ready the instant it starts — so a plugin that wedges internally (gRPC registration hangs, a
# stuck goroutine) keeps a Running, Ready, fully-available DaemonSet while kubelet quietly
# deregisters the resource. Nothing restarts it, and the only other evidence is jellyfin and tdarr
# turning unschedulable, which does not surface until they next reschedule.
#
# Comma-separated, so a second device plugin needs no code change.
K8S_EXTENDED_RESOURCES = [
    r.strip()
    for r in _env("K8S_EXTENDED_RESOURCES", "devic.es/dri").split(",")
    if r.strip()
]
# Hysteresis for check_longhorn_volumes. A node drain and the Sunday 07:30 reboot both degrade
# every volume on the departing node BY DESIGN, so a single breaching cycle must not page — 3
# cycles at the bridge cadence is longer than either takes to settle. Same shape as
# CPU_CONSECUTIVE / UPS_CONSECUTIVE.
LONGHORN_CONSECUTIVE = int(_env("LONGHORN_CONSECUTIVE", "3"))
# Crash-loop arm of the workload check: pods whose restart counter climbed more than
# K8S_RESTART_MAX inside K8S_RESTART_WINDOW page even while readiness flaps green
# (CrashLoopBackOff passes probes briefly each backoff cycle — the 2026-08-13 homepage
# incident: 31 restarts overnight, tile and replica check mostly green throughout).
# 3-in-1h ≈ steady-state backoff cadence; a legitimate deploy rollout restarts once.
K8S_RESTART_WINDOW = _env("K8S_RESTART_WINDOW", "1h")
K8S_RESTART_MAX = int(_env("K8S_RESTART_MAX", "3"))
# Recency gate on the same arm: `increase(...[1h])` is a pure lookback, so a pod that
# crash-looped and then RECOVERED keeps the monitor DOWN until the restarts age out of the
# 1h window — up to an hour of red on a healthy pod (2026-08-23 zigbee2mqtt: recovered
# 09:47, arm still firing on `restarts in window: 9`). Requiring a restart inside the last
# K8S_RESTART_RECENT_WINDOW as well clears the tile ~30m after the pod steadies while
# leaving the 1h evidence base untouched — an ongoing loop always has a recent restart.
#
# DECIDED: 30m, and the floor is the worst inter-restart SPACING, not the CrashLoopBackOff
# 5-min backoff cap. The 2026-08-13 homepage incident above spread 31 restarts over a
# night, ~15-19 min apart; a window inside that spacing goes UP in the gaps and flaps —
# and `k3s Workload Health` is `max_retries: 0` (uptime-kuma static-monitors.yaml.j2:293),
# so every flap is an immediate DOWN plus a notification. That is the crowdsec-appsec
# failure recorded at static-monitors.yaml.j2:283-289 (24 transitions in 3h). 30m clears
# two spacings and six bridge cycles. The spacing could not be re-measured — cluster
# Prometheus retains 7d and the incident is older — so this is the conservative floor.
K8S_RESTART_RECENT_WINDOW = _env("K8S_RESTART_RECENT_WINDOW", "30m")

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
# NVMe endurance ceiling (percentage_used, where 100 means the controller's rated write endurance
# is spent). Scrutiny ships this attribute with thresh=100, so its own evaluation cannot fold a
# breach into device_status until the drive is fully consumed — days of warning where the wear
# curve offers months. Verified against the live API 2026-08-22: daniel-server's SHPP41-500GM
# reads 7 at 30,959 power-on hours, daniel-box's CT1000E100SSD8 reads 0 at 576. 0 = disabled.
SCRUTINY_WEAR_MAX = float(_env("SCRUTINY_WEAR_MAX", "80"))

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
# liveness; the nut pod liveness probe owns NUT-server death), so this never double-pages those;
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

# ── log-pattern arm: a workload that is Ready and still failing ───────────────────────────
#
# Every other check here reads a metric or an API. None of them can see a service that answers
# its probes while logging stack traces — a readiness probe asks "is the port open", not "is
# the work succeeding". That is the shape of the Grafana dead-panel incident: a 1/1 pod, clean
# rollout, and 19 panels rendering nothing for 55 minutes.
#
# Both estates in one selector. `job="k8s"` is the cluster promtail's label and `job="pi"` is
# daniel-pi's, so this arm covered the Pi from the day that shipped.
LOG_ERROR_SELECTOR = _env("LOG_ERROR_SELECTOR", '{job=~"k8s|pi"}')
# Deliberately narrow. `error` is not here and must not be added: it is the single most common
# word in ordinary application logs (every 404, every retried connection), and an arm that
# pages on it is an arm that gets muted. These four mean the process itself gave up.
LOG_ERROR_PATTERN = _env(
    "LOG_ERROR_PATTERN", "(?i)(panic:|fatal|traceback|out of memory)"
)
LOG_ERROR_WINDOW = _env("LOG_ERROR_WINDOW", "1h")
# Per container, not estate-wide: one workload melting down must not be diluted by 50 quiet
# ones, and the offender's name is the whole value of the alert.
LOG_ERROR_MAX = float(_env("LOG_ERROR_MAX", "20"))
# Containers whose normal output trips the pattern. Comma-separated, case-insensitive. Keep
# this list short and say WHY in the inventory — a growing ignore list is the arm decaying.
LOG_ERROR_IGNORE = _env("LOG_ERROR_IGNORE", "")

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
# ip_ban arm of the HA monitor. HA's ban middleware runs on every request and keys on the peer
# address, so a burst of unauthenticated /api/ calls can ban an INFRASTRUCTURE ip rather than an
# attacker — on 2026-08-23 five bad calls from the node's pod-network gateway (10.42.0.1) banned
# it, and the probes then arriving from that IP got 403 and crash-looped the pod. The probes no
# longer arrive from a bannable address (they exec curl to 127.0.0.1), which fixes the crash loop
# but makes a ban SILENT: HA keeps serving, while whatever shares that source IP stays locked out.
# This arm is the visibility half.
#
# IT WATCHES THE BAN EVENT, NOT THE BAN STATE, and the difference matters when you read it at
# 03:00. `Banned IP` is logged once, at ban time. The arm therefore pages for HA_BAN_WINDOW after a
# ban is issued and then SELF-CLEARS, while the entry is still sitting in /config/ip_bans.yaml. A
# ban that predates the window — or one reloaded from that file by an HA restart, which logs
# nothing — is invisible here. So a green ha_heartbeat does NOT mean "no IP is banned"; it means
# "no ban was issued in the last HA_BAN_WINDOW".
#
# That is the only signal available: HA does not log its ongoing 403s to a banned peer, and this
# pod cannot read HA's PVC. The DURABLE artifact is the Discord notification Kuma fires on the
# down transition (push monitors run max_retries=0, so the push flips state and notifies
# immediately) — not the monitor's colour, which is transient by construction. When one fires,
# check /config/ip_bans.yaml by hand; do not wait for the monitor to go green.
HA_BAN_WINDOW = _env("HA_BAN_WINDOW", "1h")
# `container=`, NOT `app=`. Promtail's k8s stream carries container/pod/job/machine/namespace/
# service_name/stream — there is no `app` label, so `app="home-assistant"` matched no stream and
# the arm reported "no ip_ban events" forever: a monitor green because it is blind. It shipped that
# way and was caught the same day (2026-08-23) by running the selector against live Loki over a
# window containing a KNOWN ban. Same lesson as the kube-state-metrics label trap in this role's
# CLAUDE.md — a fail-open arm cannot tell "nothing to report" from "I asked the wrong question".
HA_BAN_SELECTOR = '{namespace="homelab",container="home-assistant"} |~ "Banned IP"'

# speedtest-tracker's own result rows. Empty URL/token = disabled (stays up), like HA above.
#
# WHY THIS CHECK EXISTS: a failed speedtest run wrote nothing anywhere an operator could see —
# no stdout line (the container logs only its pre-run connectivity ping), no metric, no monitor.
# The only record was a row in the app's sqlite, which nothing could read: the readonly SA holds
# no pods/exec, and the unauthenticated API is two endpoints, one of which returns a single row.
# Five of the 42 runs between 2026-08-14 and 2026-08-24 failed and none of them paged.
SPEEDTEST_URL = _env("SPEEDTEST_URL", "").rstrip("/")
# File-mounted (SPEEDTEST_TOKEN_FILE) for the same reason HA_TOKEN is: envFrom has no per-key
# filter, so a token in monitor-bridge-env is a token in every process's environment.
SPEEDTEST_TOKEN = _env_file("SPEEDTEST_TOKEN", "")
# Download floor, Mbit/s. 100 is not a target — it is the empty band. Results are bimodal by
# which Ookla server the run drew: over 2026-08-14..24 the 20 runs on server 41671 had a median
# of 910 Mbps and a worst of 119, while the 17 runs on six other servers had a median of 12.8
# and a best of 42.8. Nothing landed between 42.8 and 119, so any floor in that gap separates
# the two populations with room on both sides.
SPEEDTEST_DOWNLOAD_MIN_MBPS = float(_env("SPEEDTEST_DOWNLOAD_MIN_MBPS", "100"))
# Staleness ceiling, hours. SPEEDTEST_SCHEDULE runs every 6h, so 8 allows one missed slot plus
# slack. This arm is what notices the scheduler dying — the failure mode with no other symptom,
# since a pod that runs no tests still serves its UI and passes both probes.
SPEEDTEST_MAX_AGE_H = float(_env("SPEEDTEST_MAX_AGE_H", "8"))
# Consecutive-cycle hysteresis for the FETCH only, never for the verdict — see check_speedtest.
SPEEDTEST_CONSECUTIVE = int(_env("SPEEDTEST_CONSECUTIVE", "2"))

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
# pushes its "alive" Kuma beat on every clean run regardless of whether the Discord POST
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


# Distinct `origin` values the host-metric checks must see. node-exporter is a DaemonSet on both
# nodes, so a vector grouped by origin returning fewer than this has LOST a host, not measured a
# healthy estate — and check_disk/check_mem would report the survivor's numbers as the estate's.
# Live on 2026-08-23: daniel-box's node-exporter was unreachable for 5.4h (a one-directional UFW
# rule, k3s defaults k3s_join_server_ports) and both checks pushed OK off daniel-server alone, so
# daniel-box's host memory and /boot went unwatched behind two green tiles for the whole window.
#
# Why a floor here and not reliance on the Scrape Targets sentinel: that check keys on `up`, and
# node-exporter's normal failure mode is PER-COLLECTOR — `node_scrape_collector_success == 0`
# already returns five collectors on a host whose `up` is 1. A filesystem or meminfo collector
# failing therefore leaves `up == 1`, leaves Scrape Targets green, and drops the host from these
# two checks with nothing firing anywhere. Same shape as check_ups's partial-absence arm: never
# monitor the survivor silently. Verified before setting the floor: /, /boot and /boot/efi each
# report from both origins over the preceding 7d, so no mountpoint is legitimately single-host.
HOST_ORIGINS_MIN = int(_env("HOST_ORIGINS_MIN", "2"))
# Hysteresis, for the same reason UPS_CONSECUTIVE exists: the weekly Sunday reboot takes a node's
# node-exporter away for minutes against a 1m scrape and a 5m check loop, and a bare floor would
# page every week. Measured over the 7d to 2026-08-23, `count(node_memory_MemTotal_bytes) < 2`
# held for 66 samples and ALL 66 were inside the real outage — so the floor is quiet in steady
# state and this grace only has to cover reboots.
HOST_ORIGINS_CONSECUTIVE = int(_env("HOST_ORIGINS_CONSECUTIVE", "3"))

_host_origin_streaks: dict[str, int] = {}


def _host_origin_shortfall(key, vec, what):
    """(ok, msg) when `vec` covers fewer than HOST_ORIGINS_MIN hosts, else None.

    Passes (green, but says so) while the shortfall is younger than HOST_ORIGINS_CONSECUTIVE
    cycles, so a reboot doesn't page; fails once it persists. Any full-coverage cycle resets.
    `key` separates the streaks so disk and memory age independently.
    """
    origins = {_origin_name(labels) for labels, _ in vec}
    if len(origins) >= HOST_ORIGINS_MIN:
        _host_origin_streaks[key] = 0
        return None
    streak = _host_origin_streaks.get(key, 0) + 1
    _host_origin_streaks[key] = streak
    seen = ", ".join(sorted(origins)) or "none"
    if streak < HOST_ORIGINS_CONSECUTIVE:
        return (
            True,
            "%s: only %d of %d hosts reporting (%s), cycle %d/%d — node rebooting?"
            % (
                what,
                len(origins),
                HOST_ORIGINS_MIN,
                seen,
                streak,
                HOST_ORIGINS_CONSECUTIVE,
            ),
        )
    return (
        False,
        "%s UNKNOWN: only %d of %d hosts reporting (%s) — the absent host is NOT being checked"
        % (
            what,
            len(origins),
            HOST_ORIGINS_MIN,
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
    for mp in DISK_MOUNTPOINTS:
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
            if used_pct > DISK_MAX_PCT:
                breaching.append("%s %s %.0f%%" % (_origin_name(labels), mp, used_pct))
    if breaching:
        return False, "disk over %.0f%%: %s" % (DISK_MAX_PCT, ", ".join(breaching))
    failed = [s for s in shortfalls if not s[0]]
    if failed:
        return False, "; ".join(msg for _, msg in failed)
    if shortfalls:
        return True, "; ".join(msg for _, msg in shortfalls)
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
        if pct > MEM_MAX_PCT
    ]
    if breaching:
        return False, "mem over %.0f%%: %s" % (MEM_MAX_PCT, ", ".join(breaching))
    if short is not None:
        return short
    worst = max(pct for _, pct in vec)
    return True, "mem %.0f%%" % worst


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
        "sum by (pod) (changes(container_start_time_seconds%s[%s]))"
        % (cadvisor_sel('container!=""', 'container!="POD"'), RESTART_WINDOW)
    )
    offenders = _top_offenders(vec, "pod", lambda v: v > RESTART_MAX)
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
        "sum(increase(container_oom_events_total%s[%s])) by (pod)"
        % (cadvisor_sel('container!=""', 'container!="POD"'), OOM_WINDOW)
    )
    offenders = _top_offenders(vec, "pod", lambda v: v > 0)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) OOM-killed in %s: %s" % (
            len(offenders),
            OOM_WINDOW,
            desc,
        )
    return True, "no OOM kills in %s" % OOM_WINDOW


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
    and are ignored; if cAdvisor doesn't expose the cfs metrics both queries are empty -> green.

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
        % (sel, CPU_WINDOW, sel, CPU_WINDOW)
    )
    lost_cores = dict(
        (m.get("pod", "?"), v)
        for m, v in prom_vector(
            "sum(rate(container_cpu_cfs_throttled_seconds_total%s[%s])) by (pod)"
            % (sel, CPU_WINDOW)
        )
    )
    threshold = CPU_THROTTLE_PCT / 100.0
    offenders = []
    for m, ratio in ratio_vec:
        name = m.get("pod", "?")
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
        details = _get_json("%s/api/device/%s/details" % (SCRUTINY_URL, wwn))
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny():
    data = _get_json(SCRUTINY_URL + "/api/summary")
    summary = (data.get("data") or {}).get("summary")
    fresh_ok, fresh_msg = scrutiny_freshness(summary, SCRUTINY_MAX_AGE_H)
    if not fresh_ok:
        return False, fresh_msg
    health_ok, health_msg = scrutiny_health(summary, SCRUTINY_TEMP_MAX)
    if not health_ok:
        return False, health_msg
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and wear answers the same question device_status does — is the drive still
    # fit to hold the data on it — just months earlier. Fetched only once freshness passes, so a
    # dead collector costs no per-device calls.
    if not SCRUTINY_WEAR_MAX:
        return True, "%s; %s" % (fresh_msg, health_msg)
    wear_ok, wear_msg = scrutiny_wear_verdict(
        scrutiny_wear_devices(summary), SCRUTINY_WEAR_MAX
    )
    if not wear_ok:
        return False, wear_msg
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear_msg)


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
            UPS_CHARGE_MIN_PCT,
            UPS_RUNTIME_MIN_S,
        )
    if ok:
        _down_streaks["ups"] = 0
        return True, msg
    _down_streaks["ups"], ok, msg = down_streak(
        _down_streaks.get("ups", 0), UPS_CONSECUTIVE, msg, "grace"
    )
    return ok, msg


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
        banned = loki_count(HA_BAN_SELECTOR, HA_BAN_WINDOW)
    except Exception as e:
        return ok, "%s, ip_ban arm unavailable (%s)" % (msg, e)
    ban_ok, ban_msg = ha_ban_verdict(banned, HA_BAN_WINDOW)
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
        _down_streaks["ha"] = 0
        return with_ha_ban(True, msg)
    _down_streaks["ha"], ok, msg = down_streak(
        _down_streaks.get("ha", 0), HA_CONSECUTIVE, msg, "deploy/restart grace"
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
    if not SPEEDTEST_URL or not SPEEDTEST_TOKEN:
        return True, "speedtest monitoring disabled (no URL/token)"
    try:
        # sort=-created_at, because the default order is ASCENDING and would hand back the
        # OLDEST row in the 30-day window — a stale-forever reading that looks like a verdict.
        payload = _get_json(
            SPEEDTEST_URL + "/api/v1/results?sort=-created_at&page%5Bsize%5D=1",
            headers={
                "Authorization": "Bearer " + SPEEDTEST_TOKEN,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        _down_streaks["speedtest"], ok, msg = down_streak(
            _down_streaks.get("speedtest", 0),
            SPEEDTEST_CONSECUTIVE,
            "speedtest API unreachable: %s" % e,
            "deploy/restart grace",
        )
        return ok, msg
    _down_streaks["speedtest"] = 0
    rows = payload.get("data") or []
    return speedtest_verdict(
        rows[0] if rows else None, SPEEDTEST_DOWNLOAD_MIN_MBPS, SPEEDTEST_MAX_AGE_H
    )


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


def loki_vector(query):
    """Instant LogQL query keeping each series' labels — the loki_count peer of prom_vector.

    Not prom_vector(base=LOKI_URL): Loki's instant endpoint is /loki/api/v1/query, and
    prom_vector hardcodes /api/v1/query. Same envelope, different path.
    """
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in _instant_query(LOKI_URL, "/loki/api/v1/query", query, "loki")
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
_b2_storage = {"ts": 0.0, "ok": False, "msg": "not yet probed"}


def b2_authorize_data():
    """The parsed b2_authorize_account response. Raises on any transport/HTTP failure."""
    token = base64.b64encode(
        ("%s:%s" % (B2_PROBE_KEY_ID, B2_PROBE_APPLICATION_KEY)).encode()
    ).decode()
    return _get_json(B2_PROBE_URL, headers={"Authorization": "Basic %s" % token})


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
    cap = B2_STORAGE_CAP_BYTES if cap is None else cap
    max_pct = B2_STORAGE_MAX_PCT if max_pct is None else max_pct
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
                B2_STORAGE_MAX_PAGES,
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
    if not B2_PROBE_KEY_ID or not B2_PROBE_APPLICATION_KEY:
        return True, "B2 storage check disabled (no credentials)"
    now = now if now is not None else time.time()
    if _b2_storage["ok"] and now - _b2_storage["ts"] < B2_STORAGE_INTERVAL_S:
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
    for _ in range(B2_STORAGE_MAX_PAGES):
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
        if action in R2_CLASS_B_ACTIONS:
            class_b += requests
        elif action in R2_FREE_ACTIONS:
            continue
        elif action in R2_CLASS_A_ACTIONS:
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
    storage_max_gb = R2_STORAGE_MAX_GB if storage_max_gb is None else storage_max_gb
    class_a_max = R2_CLASS_A_MAX if class_a_max is None else class_a_max
    class_b_max = R2_CLASS_B_MAX if class_b_max is None else class_b_max
    uploads_max = R2_UPLOADS_MAX if uploads_max is None else uploads_max
    max_pct = R2_USAGE_MAX_PCT if max_pct is None else max_pct

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
        "account": json.dumps(CF_ACCOUNT_ID),
        "bucket": json.dumps(R2_BUCKET),
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
        CF_GRAPHQL_URL,
        {"query": query},
        headers={"Authorization": "Bearer %s" % CF_ANALYTICS_TOKEN},
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
    if not CF_ACCOUNT_ID or not CF_ANALYTICS_TOKEN or not R2_BUCKET:
        return True, "R2 usage check disabled (no account id / token / bucket)"
    now = now if now is not None else time.time()
    if (
        _r2_probe["ts"] is not None
        and _r2_probe["ok"]
        and now - _r2_probe["ts"] < R2_PROBE_INTERVAL_S
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
    # The second clause is the recency gate (K8S_RESTART_RECENT_WINDOW): it keeps a recovered
    # pod from holding the tile red for the rest of the 1h evidence window. `and` is a vector
    # match on the full label set, so it filters the first clause's series rather than
    # replacing them — the offender labels reaching the verdict are unchanged.
    restart_offenders = prom_vector(
        "increase(kube_pod_container_status_restarts_total[%s]) > %d"
        " and increase(kube_pod_container_status_restarts_total[%s]) > 0"
        % (K8S_RESTART_WINDOW, K8S_RESTART_MAX, K8S_RESTART_RECENT_WINDOW),
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
    ok, msg = k8s_workloads_verdict(
        total,
        offenders,
        K8S_MIN_WORKLOADS,
        restart_offenders,
        ds_total,
        ds_offenders,
        K8S_MIN_DAEMONSETS,
    )
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and this arm answers the same question the DaemonSet arm does — is the
    # cluster still able to run the workloads that depend on it.
    advertised = {}
    for resource in K8S_EXTENDED_RESOURCES:
        advertised[resource] = len(
            prom_vector(
                'kube_node_status_allocatable{resource="%s"} > 0'
                % ksm_resource_label(resource),
                base=CLUSTER_PROM_URL,
                source="cluster prometheus",
            )
        )
    res_ok, res_msg = extended_resource_verdict(
        K8S_EXTENDED_RESOURCES,
        advertised,
        prom_scalar(
            "count(kube_node_status_allocatable)",
            base=CLUSTER_PROM_URL,
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
    if not LOG_ERROR_SELECTOR:
        return ok, msg
    ignore = {n.strip().lower() for n in LOG_ERROR_IGNORE.split(",") if n.strip()}
    try:
        matches, total = log_error_counts(
            LOG_ERROR_SELECTOR, LOG_ERROR_PATTERN, LOG_ERROR_WINDOW
        )
    except Exception as e:
        return ok, "%s, log-error arm unavailable (%s)" % (msg, e)
    log_ok, log_msg = log_error_verdict(
        matches, total, LOG_ERROR_MAX, LOG_ERROR_WINDOW, ignore
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
    if not CLUSTER_PROM_URL:
        return True, "cluster target check disabled (no CLUSTER_PROMETHEUS_URL)"
    vec = prom_vector(
        'up{origin!="daniel-server"}',
        base=CLUSTER_PROM_URL,
        source="cluster prometheus",
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
        _down_streaks.get("discord", 0), DISCORD_CONSECUTIVE, msg, "transient grace"
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
            LONGHORN_CONSECUTIVE,
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
        LONGHORN_CONSECUTIVE,
        "of %d volume(s): %s" % (int(volumes), "; ".join(parts)),
        "drain/reboot grace",
    )
    return ok, msg


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
    (
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    ("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    ("scrutiny", _env("KUMA_PUSH_SCRUTINY", ""), check_scrutiny),
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
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory"}),
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
        _get_json("%s/api/push/%s?%s" % (KUMA_URL, token, qs))
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
                    name, ok, msg, GRACE_CYCLES, _grace_streaks
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
        % (INTERVAL, once, len(enabled), len(CHECKS))
    )
    while True:
        run_once()
        bridge_common.touch_heartbeat(HEARTBEAT_FILE)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
