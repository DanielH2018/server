"""Env-derived configuration for monitor-bridge — every threshold, URL, credential and window.

Read it through the module object (`import bridge_config as cfg` then `cfg.PROM_URL`), never
by `from bridge_config import PROM_URL`. A from-import copies the value into the importer's
globals at import time, so a test that patches `bridge_config.PROM_URL` afterwards would change
nothing the importer reads. Tests patch the constants HERE, on this module, and the checks that
read them look them up at call time. ansible/tests/services/test_bridge_patch_boundary.py enforces the
qualified read; ansible/tests/services/test_monitor_bridge_modules.py checks that every patched name is
bound in the module the test patches it on.

The two tests that re-derive `PROM_ORIGIN` from the environment `importlib.reload()` THIS
module. `reload` re-executes only the module it is given, which is why the constants and the
env reads live together here rather than beside the check that consumes each one.

Constants only. The mutable per-check state (`_n8n_streaks`, `_cadvisor_streaks`,
`_host_origin_streaks`, `_down_streaks`) stays with the code that mutates it.
"""

import os

from bridge_common import _env


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

# Bazarr's link to Sonarr and Radarr. Bazarr holds its OWN copies of their API keys, in its
# config on the bazarr-config PVC and entered through its UI — so no Ansible template carries
# them and no deploy updates them. On 2026-08-29 a rotation swept the eight templated
# consumers, missed Bazarr, and its SignalR client dropped into a reconnect loop that leaked
# 173 MiB to its 1Gi cap in 90 minutes and OOM-killed it. The ONLY signal was the "k3s
# Container OOM" tile, which clears one hour after the kill and takes the evidence with it,
# leaving Bazarr fetching no subtitles, silently.
#
# NOTE the header spelling: `X-API-KEY`, not the `X-Api-Key` Sonarr and Radarr take. Verified
# against the live app 2026-08-29 — a request with no key returns 401, so the key is doing work.
BAZARR_URL = _env("BAZARR_URL", "http://bazarr:6767").rstrip("/")
BAZARR_API_KEY = _env("BAZARR_API_KEY", "")

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
ETCD_DRILL_STATE_DIR = _env("ETCD_DRILL_STATE_DIR", "/etcd-drill-state")
# DECIDED: 8 days, DERIVED from the drill's cadence rather than picked round. The cron is
# k3s_etcd_restore_drill_cron = "20 10 * * 1" — weekly, Monday 10:20 — so anything over 7 days
# means a scheduled run did not happen or did not pass. 8 gives one day of slack for the run
# itself and for a check that evaluates just before the window, and no more: a wider grace and
# the monitor clears the very miss it exists to catch, which is how a 24h grace against a 23h
# gap read green on 2026-08-25. Move this ONLY together with the cron; the two are pinned to
# each other by test_etcd_drill_grace_is_derived_from_the_cron.
ETCD_DRILL_MAX_AGE_S = float(_env("ETCD_DRILL_MAX_AGE_DAYS", "8")) * 86400
# The staging-gate backfill ratchet's run-recency window. It shares GITOPS_STATE_DIR: the unit
# writes its heartbeat and Ansible writes its armed marker into /var/lib/gitops-deploy, which
# this pod already hostPath-mounts for the gitops pair.
#
# DECIDED: 150 minutes, DERIVED from the timer rather than picked round. staging-backfill.timer
# is OnUnitActiveSec=1h with RandomizedDelaySec=10min, and TimeoutStartSec=25min bounds the run
# itself, so the worst-case gap between two heartbeats is about 95 minutes. 150 clears that with
# slack and still falls short of two cadences (190 min) — a window that spans two would tolerate
# a fully missed run, which is the miss this check exists to catch. Move it only with the timer;
# test_staging_backfill_window_is_derived_from_the_timer pins the pair.
STAGING_BACKFILL_MAX_AGE_S = float(_env("STAGING_BACKFILL_MAX_AGE_MIN", "150")) * 60
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
# though the k8s Secret feeds it the `kopia_b2_*` values — those are LONGHORN's B2 credentials,
# not Kopia's (ADR-0014; Kopia retired 2026-08-13, the key name outlived it because renaming means
# a rotation). This probe only needs to authenticate, so swapping in a scoped read-only key later
# is an inventory edit, not a code one.
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
# The TTL for a failure that never reached B2 (DNS, connect, timeout). Deliberately NOT
# B2_PROBE_INTERVAL_S: the whole reason that interval is long is that a probe costs a transaction,
# and a connection that never landed costs nothing, so the argument above does not apply to it.
# One INTERVAL, so the next cycle re-probes and the recovery is not held back — see b2_reachable.
B2_TRANSPORT_RETRY_S = float(_env("B2_TRANSPORT_RETRY_S", str(INTERVAL)))

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
# (claude-otel/templates/prometheus.yaml.j2:202, the `node` job); the cadvisor job never was, which is the whole
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
# Coverage floor for the three cAdvisor checks (restarts/oom/cpu), which filter a per-pod vector
# down to offenders and so cannot tell "quiet" from "gone". Reasoning and the measurements behind
# the value: cadvisor_coverage_shortfall in verdicts_cluster.py.
CADVISOR_PODS_MIN = int(_env("CADVISOR_PODS_MIN", "20"))
# Hysteresis for the same reason HOST_ORIGINS_CONSECUTIVE exists: a kubelet restart takes a node's
# cAdvisor away briefly, and three monitors going down together on one transient is the alert storm
# the gates elsewhere in this file exist to prevent.
CADVISOR_CONSECUTIVE = int(_env("CADVISOR_CONSECUTIVE", "2"))


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
# Filesystem fullness of the cluster's PersistentVolumeClaims (check_pvc_fullness). A separate
# arm from check_disk rather than another DISK_MOUNTPOINTS entry: a Longhorn PVC is its own
# filesystem at a FIXED capacity, so it cannot borrow the host's free space and a full one is
# invisible to every mountpoint query. 85 rather than DISK_MAX_PCT's 90 because a PVC cannot be
# grown by deleting something elsewhere — the operator has to expand the volume, and the alert
# has to arrive while that is still unhurried work. Measured 2026-09-01: the fullest claim was
# uptime-kuma-data at 38.6%, and the smallest genuine claim is 973 MiB, where 85% leaves 146 MiB
# of headroom against 97 MiB at 90%.
PVC_MAX_PCT = float(_env("PVC_MAX_PCT", "85"))
# Claims this arm must NOT scan, comma-separated bare claim names. media-data is a `local` PV at
# /srv/media on daniel-box (k8s/media-volume/templates/pv.yaml.j2), i.e. the `/` filesystem
# check_disk already watches — including it would page twice for one full disk. Every other claim
# is Longhorn with a filesystem of its own. Keep this short and say WHY in the inventory, like
# LOG_ERROR_IGNORE: a growing exclusion list means the arm is decaying.
PVC_EXCLUDE = [
    c.strip() for c in _env("PVC_EXCLUDE", "media-data").split(",") if c.strip()
]
# Coverage floor, in CLAIMS not series — see check_pvc_fullness for why the two differ. NOT a
# conservative under-count like CADVISOR_PODS_MIN, because the degraded state here is a specific
# number rather than an empty vector. The two scrape jobs cover unequally (measured 2026-09-01,
# groups not series): kubelet alone reports all 43 claims, apiserver alone reports 27. So the
# apiserver job dying costs no coverage at all, and the ONLY hazard is the kubelet job dying,
# which leaves 27 claims answering while daniel-server's go dark. A floor at or under 27 reads
# that as healthy — the same partial blindness HOST_ORIGINS_MIN exists for. 32 is strictly above
# the 27-claim survivor and 11 below the live 43, so it fires on that outage and still tolerates
# a dozen services being retired.
PVC_MIN_CLAIMS = int(_env("PVC_MIN_CLAIMS", "32"))
# Hysteresis on the coverage floor only. A kubelet restart or a node drain drops a node's volume
# stats for a cycle or two, and that must not page; a fullness breach gets no grace because it is
# monotonic rather than flappy.
PVC_CLAIMS_CONSECUTIVE = int(_env("PVC_CLAIMS_CONSECUTIVE", "3"))
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

# Board and CPU temperature from node-exporter's hwmon collector. Drives are NOT read here —
# check_scrutiny owns them (its device_status folds the SMART temperature attribute), so
# HWMON_TEMP_EXCLUDE_CHIP drops the nvme chips and the two checks never page for one condition.
#
# Each sensor gets a limit from ONE of two arms, and the arms are exhaustive over the scraped
# vector — every non-excluded series is covered, which is what test_host_temp_covers_every_sensor
# pins. Measured live 2026-08-28: 21 temp series (daniel-server 12, daniel-box 7, daniel-pi 2).
#
#   1. The sensor's own declared max, when it declares a PLAUSIBLE one: page at
#      HWMON_TEMP_RATIO of it. Preferred — coretemp declares 100, the daniel-server NVMe 85.85.
#   2. HWMON_TEMP_FALLBACK_C, a flat ceiling, for every sensor that does not.
#
# DECIDED: the declared max is sanity-bounded rather than trusted, because three of the ten
# max-declaring sensors declare 65261.85 (0xFFFF sentinel, an undeclared max encoded as a
# number): daniel-server nvme temp2/temp3 and daniel-box nvme temp3. Ratio-of-max against that
# is unreachable, so those sensors would read green through a fire — the inert-check class in
# [[an-optimisation-can-land-green-and-be-inert]]. A max outside
# (HWMON_TEMP_MIN_PLAUSIBLE_C, HWMON_TEMP_MAX_PLAUSIBLE_C] is therefore treated as UNDECLARED and
# falls to arm 2. This is also why the fallback arm is not optional: without it, 14 of 21 sensors
# — including BOTH daniel-pi sensors, on the host with no fan — carry no limit at all.
HWMON_TEMP_RATIO = float(_env("HWMON_TEMP_RATIO", "0.90"))
HWMON_TEMP_FALLBACK_C = float(_env("HWMON_TEMP_FALLBACK_C", "85"))
HWMON_TEMP_MIN_PLAUSIBLE_C = float(_env("HWMON_TEMP_MIN_PLAUSIBLE_C", "20"))
HWMON_TEMP_MAX_PLAUSIBLE_C = float(_env("HWMON_TEMP_MAX_PLAUSIBLE_C", "150"))
HWMON_TEMP_EXCLUDE_CHIP = _env("HWMON_TEMP_EXCLUDE_CHIP", "nvme_")
# Hysteresis: a transcode or a compile spikes coretemp for one scrape. 3 cycles at the loop
# cadence is sustained heat, not a burst.
HWMON_TEMP_CONSECUTIVE = int(_env("HWMON_TEMP_CONSECUTIVE", "3"))

# Host-coverage floor for the thermal check, the peer of HOST_ORIGINS_MIN and deliberately a
# DIFFERENT number. Until 2026-08-29 hwmon_temp_verdict paged only on a fully empty vector, so
# any non-empty subset passed: lose one host's hwmon collector and the other two answered "all
# below limit" for the whole estate, forever. A total node-exporter death is already caught by
# check_cluster_targets; the gap is the PARTIAL failure — node-exporter up, one collector blind —
# which check.py's own HOST_ORIGINS_MIN comment names as node-exporter's normal failure mode.
#
# 3 rather than the shared 2 because all three hosts declare non-excluded sensors, measured live
# 2026-08-29 after HWMON_TEMP_EXCLUDE_CHIP: daniel-server 9, daniel-box 5, daniel-pi 2. The
# shared floor of 2 would be met by any two of them, which is exactly the state this must catch.
HWMON_TEMP_ORIGINS_MIN = int(_env("HWMON_TEMP_ORIGINS_MIN", "3"))
# Its own grace, longer than HOST_ORIGINS_CONSECUTIVE, because the third host is daniel-pi and
# the Pi drops out for longer than either amd64 node. Measured over the 7d to 2026-08-29 at a 5m
# step: 1054 samples, coverage below 3 in 6 of them, all daniel-pi (1048/1054 present), and the
# worst 30m window held 4 consecutive short samples — about 20 minutes. The shared grace of 3
# cycles is 15 minutes at INTERVAL=300, so it would have paged once in that week on a healthy
# estate. 5 cycles is 25 minutes: one cycle of margin over the observed worst case.
HWMON_TEMP_ORIGINS_CONSECUTIVE = int(_env("HWMON_TEMP_ORIGINS_CONSECUTIVE", "5"))

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
#   arm 3 (the Pi): both arms above are CLUSTER streams. daniel-pi runs its own promtail,
#   stamping `job="pi"` — the label LOG_ERROR_SELECTOR already knows about. Nothing counted
#   it, so the Pi's promtail could die with every cluster stream still flowing and both arms
#   green: the Pi's logs simply stop arriving and no monitor says so (2026-08-25 review M-11).
#   The window is the tolerant one, and for a stronger reason than arm 1's: the Pi is a
#   Zero 2 W running five LAN-only containers, so its log volume is genuinely low and bursty.
#   `machine!="daniel-pi"` is the SAME masking rule as the docker_sd exclusion above, applied
#   to a second source that has since started writing into `job="syslog"`. daniel-pi's promtail
#   now ships its two health crons' verdict lines under that job (roles/containers/promtail,
#   the pi-health scrape job) so `probe.py alerts` can reconstruct a Pi episode. Those ~576
#   lines/day arrive from a HOST OUTSIDE the cluster, so a total cluster file-tail outage would
#   no longer reach zero and arm 1 would never fire — the Pi would be holding the alert open on
#   behalf of the streams it knows nothing about. Loki's `!=` also matches a stream that has no
#   `machine` label at all, so the cluster's own authlog/syslog/traefik streams are unaffected.
#   The Pi's own liveness stays covered by arm 3.
LOKI_STREAM = _env(
    "LOKI_STREAM", '{job=~"authlog|syslog|traefik", machine!="daniel-pi"}'
)
LOKI_DOCKER_STREAM = _env("LOKI_DOCKER_STREAM", '{container=~".+"}')
LOKI_PI_STREAM = _env("LOKI_PI_STREAM", '{job="pi"}')
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
# `name:port` pairs for the Pi containers that publish a port, rendered from daniel-pi's
# containers_list (every entry with a `port`) so the set cannot drift from the inventory.
# Empty = the port arm is disabled, like PI_GLANCES_URL disables the whole check.
PI_PUBLISHED_PORTS = tuple(
    (pair.split(":", 1)[0].strip(), int(pair.split(":", 1)[1]))
    for pair in _env("PI_PUBLISHED_PORTS", "").split(",")
    if ":" in pair
)
PI_PORT_TIMEOUT = float(_env("PI_PORT_TIMEOUT", "3"))
# A Pi deploy recreates containers, so their ports are genuinely closed for a few seconds.
# Two cycles of grace, same idiom as HA_CONSECUTIVE; a detached container persists until
# someone recreates it and still pages.
PI_PORTS_CONSECUTIVE = int(_env("PI_PORTS_CONSECUTIVE", "2"))

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
