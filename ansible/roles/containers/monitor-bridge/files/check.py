#!/usr/bin/env python3
"""monitor-bridge — evaluate homelab health checks and push results to Uptime Kuma.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop). Config is entirely env-driven so this file stays plain/testable.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import json
import os
import smtplib
import ssl
import statistics
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
# bridge's first cycle after a host reboot runs before the heavy apps it polls (kopia, n8n,
# sonarr/radarr, the Pi glances) finish starting, so an un-graced reach-out check flips its
# max_retries=0 push monitor DOWN on that one transient cycle and pages, then recovers next cycle —
# the weekly-reboot noise. Like HA_CONSECUTIVE, only the GRACE_CYCLES'th consecutive down pages; a
# genuinely-down dependency still alerts after ~one extra INTERVAL, and one ok resets the streak.
GRACE_CYCLES = int(_env("GRACE_CYCLES", "2"))
# Touched after every completed cycle; the container healthcheck compares its mtime
# against ~3×INTERVAL. PID death already restarts the container, but a HANG only shows
# up as push silence in Kuma — the healthcheck lets autoheal restart on that too.
HEARTBEAT_FILE = _env("HEARTBEAT_FILE", "/tmp/heartbeat")
PROM_URL = _env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
KOPIA_URL = _env("KOPIA_URL", "http://kopia:51515").rstrip("/")
KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
LOKI_URL = _env("LOKI_URL", "http://loki:3100").rstrip("/")

BACKUP_PATH = _env("BACKUP_SOURCE_PATH", "/data/home/ubuntu/server/containers")
BACKUP_MAX_AGE_H = float(_env("BACKUP_MAX_AGE_H", "30"))
# Snapshot-size regression guard (folded into check_backup). A kopiaignore over-match or a vanished
# bind mount can silently drop a service from the CONFIG-ONLY backup source: the snapshot still
# completes with errorCount 0 and a fresh timestamp, so the freshness/error check above stays GREEN
# while the offsite copy quietly loses a service (the recurring bare-`data/` incident class, see
# kopiaignore.j2's own changelog). Nothing else catches a SHRINK — the B2 monitors track billable-bytes
# GROWTH (a shrink reads greener) and the monthly restore drill exercises only one rotated service. We
# compare the latest snapshot's total files/bytes against a rolling floor: BACKUP_SIZE_DROP_PCT below
# the MEDIAN of the trailing snapshots (the median absorbs the routine one-off drops from exclusion
# tuning, so only a sharp anomalous drop pages). Sourced from Kopia's own snapshot history
# (/api/v1/snapshots), so the check stays stateless.
BACKUP_SIZE_DROP_PCT = float(_env("BACKUP_SIZE_DROP_PCT", "20"))
BACKUP_SIZE_MIN_HISTORY = int(_env("BACKUP_SIZE_MIN_HISTORY", "3"))
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
N8N_URL = _env("N8N_URL", "http://n8n:5678").rstrip("/")
N8N_API_KEY = _env("N8N_API_KEY", "")
N8N_FAIL_WINDOW = _env("N8N_FAIL_WINDOW", "15m")
N8N_FAIL_MAX = float(_env("N8N_FAIL_MAX", "0"))

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
RENOVATE_STATE_DIR = _env("RENOVATE_STATE_DIR", "/renovate-state")
RENOVATE_MAX_AGE_S = float(_env("RENOVATE_MAX_AGE_MIN", "2160")) * 60

# Monthly kopia restore drill: the host cron (kopia-restore-drill.sh, kopia role)
# writes {"ts": epoch, "ok": bool, "msg": str} after each run; we alert on failure,
# staleness (cron broken / never ran), or a missing/corrupt state file.
RESTORE_DRILL_STATE = _env("RESTORE_DRILL_STATE", "/restore-drill/state.json")
RESTORE_DRILL_MAX_AGE_S = float(_env("RESTORE_DRILL_MAX_AGE_D", "35")) * 86400

# Daily wg-easy Pi-peer backup pull: the wg-easy role's daniel-server host cron
# (wg-easy-pull-pi-peers.sh) rsyncs the Pi's WireGuard peer configs (wg0.conf/wg0.json — private
# keys a redeploy can't rebuild) into Kopia scope and writes {"ts": epoch, "ok": bool, "msg": str}.
# It's the only Pi state pulled into the backup AND was the only backup cron with no watchdog: the
# pull uses no --delete, so a broken pull (Pi unreachable, SSH/sudo break) leaves the last-good copy
# in place and Backup Freshness stays green while the peers silently go stale. We alert on a FAILED
# pull, staleness (cron broken / never ran), or a missing/corrupt state file. 2.5d staleness = two
# missed daily runs + slack (same as the daily b2-usage / maintenance crons).
PI_PEERS_STATE = _env("PI_PEERS_STATE", "/pi-peers/state.json")
PI_PEERS_MAX_AGE_S = float(_env("PI_PEERS_MAX_AGE_D", "2.5")) * 86400

# Every-5-min CrowdSec home-IP allowlist updater (traefik role's crowdsec-update-home-allowlist.sh):
# keeps the operator's current home public IP in CrowdSec's `home-ips` allowlist so the public path
# from home doesn't trip the WAF. It writes {"ts": epoch, "ok": bool, "msg": str} on EVERY run (incl.
# the common IP-unchanged fast path). It was the last self-`logger`ing cron with no watchdog — a silent
# failure (ipify unreachable, cscli error) just meant occasional 403s on the next IP rotation, invisible
# until noticed. We alert on a FAILED run or staleness (cron broken / never ran). 30 min = 6 missed
# 5-min runs; the fast-path heartbeat keeps a healthy no-op green.
HOME_ALLOWLIST_STATE = _env("HOME_ALLOWLIST_STATE", "/home-allowlist/state.json")
HOME_ALLOWLIST_MAX_AGE_S = float(_env("HOME_ALLOWLIST_MAX_AGE_MIN", "30")) * 60

# DOCKER-USER origin-lock watchdog: the traefik role's docker-user-verify.sh cron (every 15 min, as
# root) reads the LIVE iptables DOCKER-USER chain and asserts the terminal DROP for :80/:443 plus a
# RETURN allow are present, then writes {"ts": epoch, "ok": bool, "msg": str}. The seed/re-assert
# systemd units apply the rules but write no state, so this is the only signal that the origin lock is
# ACTUALLY applied — a chain flushed after boot (docker network reload, manual iptables -F, a Docker
# upgrade seeding a preempting RETURN) would otherwise leave 80/443 reachable direct (Cloudflare/
# CrowdSec bypass) invisibly. ok=false = the live assert failed; staleness = the verify cron stopped.
# 45 min = 3 missed 15-min runs.
DOCKER_USER_STATE = _env("DOCKER_USER_STATE", "/docker-user/state.json")
DOCKER_USER_MAX_AGE_S = float(_env("DOCKER_USER_MAX_AGE_MIN", "45")) * 60

# Weekly Cloudflare-IP drift check (traefik role's cloudflare-ip-drift.sh cron): compares the hardcoded
# cloudflare_ips allowlist (group_vars/all.yml) — which gates BOTH Traefik's forwardedHeaders.trustedIPs
# AND the DOCKER-USER origin-lock DROP — against Cloudflare's published ranges and writes {"ts","ok",
# "msg"}. A stale list silently DROPs a client arriving on a newly-added CF range at the edge firewall
# (no log trail); Cloudflare changes these rarely, so this is a low-frequency safety net. We alert on
# drift (ok=false), a failed fetch, or >10d staleness (one missed weekly run + slack).
CLOUDFLARE_DRIFT_STATE = _env("CLOUDFLARE_DRIFT_STATE", "/cloudflare-drift/state.json")
CLOUDFLARE_DRIFT_MAX_AGE_S = float(_env("CLOUDFLARE_DRIFT_MAX_AGE_D", "10")) * 86400

APPSEC_STATE = _env("APPSEC_STATE", "/crowdsec-appsec/state.json")
APPSEC_MAX_AGE_S = float(_env("APPSEC_MAX_AGE_MIN", "45")) * 60

# Weekly kopia snapshot verify: the host cron (kopia-verify.sh, kopia role) writes
# {"ts": epoch, "ok": bool, "msg": str} after each run; we alert on a FAILED verify
# (detected bit-rot / an unreadable blob — failures the old `| logger` cron silently
# swallowed), staleness (cron broken / never ran), or a missing/corrupt state file.
# This is the verify TIER of the three-tier backup assurance (snapshot freshness →
# weekly verify → monthly restore drill) — the only one that previously had no monitor.
# 10d staleness = one missed weekly run + slack.
VERIFY_STATE = _env("VERIFY_STATE", "/verify/state.json")
VERIFY_MAX_AGE_S = float(_env("VERIFY_MAX_AGE_D", "10")) * 86400

# Quarterly kopia DEEP content verify: the kopia role's content-verify.sh cron (1st of Mar/Jun/Sep/
# Dec) runs `kopia snapshot verify --verify-files-percent=25` and writes {"ts": epoch, "ok": bool,
# "msg": str}. The deep tier below the weekly 1% verify — a mis-purge / blob-accounting bug the 1%
# sample keeps missing gets caught within a quarter. ok=false = the deep verify FAILED; staleness =
# the quarterly cron stopped. 100d tolerates the ~92d max gap between quarters + margin (a MISSED
# quarter -> ~183d -> pages).
CONTENT_VERIFY_STATE = _env("CONTENT_VERIFY_STATE", "/content-verify/state.json")
CONTENT_VERIFY_MAX_AGE_S = float(_env("CONTENT_VERIFY_MAX_AGE_D", "100")) * 86400

# Hourly disk-autoprune host cron (autofix-bridge role): writes {"ts": epoch, "ok": bool, "msg":
# str} after checking `/` used% against a threshold and, if crossed, running a conservative
# docker/builder/container prune. Same state-file idiom as verify/pi_peers. ok=false means the
# prune command itself errored — a disk still full of real data after a clean prune is Root
# Disk's alert, not this one. 3h staleness = 3x the hourly cron + slack.
DISK_PRUNE_STATE = _env("DISK_PRUNE_STATE", "/autofix-disk/state.json")
DISK_PRUNE_MAX_AGE_S = float(_env("DISK_PRUNE_MAX_AGE_H", "3")) * 3600

# Daily kopia FULL-maintenance freshness: the host cron (kopia-maintenance-check.sh, kopia role)
# queries `kopia maintenance info --json` and writes {"ts": epoch, "ok": bool, "msg": str} after
# deciding whether full maintenance is healthy (enabled + owned + next full run not overdue +
# newest run succeeded). Full maintenance is what GCs expired blobs from B2, so a stall is the
# upstream CAUSE the b2_usage check only catches weeks later as a downstream symptom. We alert on
# an UNHEALTHY/stalled maintenance, staleness (cron broken / never ran), or a missing/corrupt
# state file. 2.5d staleness = two missed daily runs + slack.
MAINTENANCE_STATE = _env("MAINTENANCE_STATE", "/maintenance/state.json")
MAINTENANCE_MAX_AGE_S = float(_env("MAINTENANCE_MAX_AGE_D", "2.5")) * 86400

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

# B2 usage growth TREND. The same daily host cron (kopia-b2-usage / b2-usage.sh) also exports
# the billable bytes as the Prometheus gauge `kopia_b2_billable_bytes` (node-exporter textfile).
# The B2_USAGE check above fires only at an ABSOLUTE 85% of the cap — no runway warning for fast
# growth still under that line (the recurring hidden-version / LiveSync-churn incidents that ate
# the headroom in days). We fit a linear trend over B2_TREND_WINDOW and project it forward with
# predict_linear: `down` when the bucket is on track to hit the cap within B2_TREND_HORIZON_D
# days. Prom-dependent (so it's suppressed under the Prometheus-reachability gate, below). A
# missing gauge (cron not exporting / textfile collector broken) reads as unavailable -> down,
# distinct from B2_USAGE's state.json staleness.
B2_TREND_METRIC = _env("B2_TREND_METRIC", "kopia_b2_billable_bytes")
B2_TREND_WINDOW = _env("B2_TREND_WINDOW", "7d")
B2_TREND_HORIZON_D = float(_env("B2_TREND_HORIZON_D", "7"))
# Textfile-freshness guard for the trend gauge. node-exporter serves the last written textfile
# value on EVERY scrape, so if b2-usage.sh's atomic .prom write fails (mv into a re-rooted/full
# textfile dir) while write_state still writes state.json fresh, the gauge FREEZES: B2 Storage
# Usage stays green (state.json fresh) AND the frozen gauge reads flat -> this trend check reads
# up — a silent blind spot. The collector's per-file `node_textfile_mtime_seconds` catches exactly
# that: go `down` (fail-stale) once the kopia_b2 textfile's mtime is older than this. 2.5 d mirrors
# B2_USAGE's daily-cron staleness. A missing mtime metric (textfile collector absent) skips the
# guard — the `current` gauge would already be None -> "unavailable" above — so it never false-pages.
B2_TREND_MTIME_QUERY = _env(
    "B2_TREND_MTIME_QUERY", 'node_textfile_mtime_seconds{file=~".*kopia_b2.*"}'
)
B2_TREND_MAX_AGE_S = float(_env("B2_TREND_MAX_AGE_D", "2.5")) * 86400

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

# Recyclarr sync health: recyclarr runs `recyclarr sync` via supercronic on an @daily schedule;
# the container healthcheck only proves supercronic (the scheduler) is alive, so a sync that
# ERRORS shows up nowhere but the logs (the silent 2026-06-10 v8-major breakage class). /cron.sh
# ends with `recyclarr sync`, so its exit code is recyclarr's and supercronic logs `job succeeded`
# (exit 0) / `job failed` (non-zero). We count both in Loki over a window covering one daily run
# + slack: any `job failed` OR zero `job succeeded` -> down. Reuses the existing Loki query path.
RECYCLARR_LOKI_SELECTOR = _env("RECYCLARR_LOKI_SELECTOR", '{container="recyclarr"}')
RECYCLARR_WINDOW = _env("RECYCLARR_WINDOW", "26h")

# Janitorr scheduled-cleanup error watchdog: janitorr's healthcheck only proves the JVM is alive
# (`grep java /proc/1/comm`), so a scheduled cleanup that throws — a failed delete, a bad config, an
# internal bug — logs ERROR and is otherwise invisible, and janitorr DELETES REAL MEDIA. The one
# benign, recurring ERROR is the documented post-boot race: an @Scheduled cleanup fires before
# jellyfin/sonarr/radarr finish loading -> FeignException 503, self-heals next cycle (janitorr's
# CLAUDE.md; a RestartCount ~4 after a reboot is EXPECTED). That ERROR line is generic ("Unexpected
# error occurred in scheduled task"), identical to a real failure and with the exception type on a
# separate Loki line, so it can't be filtered by content — we discriminate by TIME via the
# container's Prometheus uptime: within JANITORR_STARTUP_GRACE_S of startup we don't count, and past
# it we count only over the post-startup slice (min(window, uptime - grace)) so the boot race can
# never be in-window. Prom-dependent (uptime) AND Loki-dependent (count) — suppressed under either gate.
JANITORR_LOKI_SELECTOR = _env("JANITORR_LOKI_SELECTOR", '{container="janitorr"}')
JANITORR_ERROR_MATCH = _env(
    "JANITORR_ERROR_MATCH", "Unexpected error occurred in scheduled task"
)
JANITORR_WINDOW = _env("JANITORR_WINDOW", "12h")
JANITORR_STARTUP_GRACE_S = float(_env("JANITORR_STARTUP_GRACE_S", "600"))


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
    srcs = [
        s
        for s in sources_json.get("sources", [])
        if s.get("source", {}).get("path") == path
    ]
    if not srcs:
        raise LookupError("no Kopia source for %s" % path)
    last = srcs[0].get("lastSnapshot")
    if not last:
        raise LookupError("no snapshot recorded yet")
    end = last.get("endTime") or last.get("startTime")
    age_h = (now - parse_rfc3339(end)).total_seconds() / 3600.0
    errs = int(last.get("stats", {}).get("errorCount", 0))
    return age_h, errs


def backup_source(sources_json, path):
    """Return the {host, userName, path} identity dict for the Kopia source matching `path`.

    Raises LookupError if there's no such source (same contract as backup_age_hours). The host +
    userName are needed to query that source's snapshot HISTORY (/api/v1/snapshots), which only
    /api/v1/sources exposes.
    """
    for s in sources_json.get("sources", []):
        src = s.get("source", {})
        if src.get("path") == path:
            return src
    raise LookupError("no Kopia source for %s" % path)


def backup_size_regression(snapshots, drop_pct, min_history):
    """Pure: has the latest snapshot's file count or total size dropped below a rolling floor? (ok, msg).

    `snapshots` is Kopia's /api/v1/snapshots list for ONE source, each carrying a `summary` with the
    whole-tree totals `files` and `size` — NOT `stats.fileCount`, which counts only the newly-hashed
    non-cached files (e.g. 100 of 8113) and swings wildly by design. A config-only source grows
    steadily, so we floor each metric at `drop_pct` below the MEDIAN of the trailing history (excluding
    the latest): the median absorbs the routine one-off dips from exclusion tuning, so only a sharp
    anomalous drop — a service silently leaving backup scope — pages. Fewer than `min_history` prior
    snapshots means too little baseline to judge, so it stays up.
    """
    entries = []
    for s in snapshots or []:
        summ = s.get("summary") or {}
        files, size = summ.get("files"), summ.get("size")
        if files is None or size is None:
            continue
        entries.append(
            (s.get("endTime") or s.get("startTime") or "", int(files), int(size))
        )
    entries.sort(
        key=lambda e: e[0]
    )  # chronological; endTimes are same-format UTC, so lexical == temporal
    if len(entries) < min_history + 1:
        return True, "size guard skipped — only %d snapshot(s) of history" % len(
            entries
        )
    latest_files, latest_size = entries[-1][1], entries[-1][2]
    med_files = statistics.median(f for _, f, _ in entries[:-1])
    med_size = statistics.median(sz for _, _, sz in entries[:-1])
    floor = 1 - drop_pct / 100.0
    if med_files > 0 and latest_files < med_files * floor:
        return False, (
            "snapshot file count dropped to %d (trailing median %g, -%.0f%%, floor %.0f%%) "
            "— a service may have silently left backup scope"
            % (latest_files, med_files, 100 * (1 - latest_files / med_files), drop_pct)
        )
    if med_size > 0 and latest_size < med_size * floor:
        return False, (
            "snapshot size dropped to %.2fGB (trailing median %.2fGB, -%.0f%%, floor %.0f%%) "
            "— a service may have silently left backup scope"
            % (
                latest_size / 1e9,
                med_size / 1e9,
                100 * (1 - latest_size / med_size),
                drop_pct,
            )
        )
    return True, "size ok (%d files, %.2fGB, >= %.0f%% of trailing median)" % (
        latest_files,
        latest_size / 1e9,
        100 * floor,
    )


def backup_presence_regression(listings, min_history):
    """Pure: did a consistently-backed-up service directory vanish from the latest snapshot? (ok, msg).

    `listings` is a chronological list (latest LAST) of {top_level_dir_name: recursive_file_count}
    maps — one per snapshot, from each snapshot's root directory listing. A service is "present" in a
    snapshot when its dir is there with > 0 files. The expected set is every dir present in ALL of the
    trailing `min_history` snapshots before the latest; we page if any expected dir is missing (or
    dropped to 0 files) in the latest — a whole service silently leaving backup scope, which the
    aggregate backup_size_regression can't see once the service is small (< the 20% floor; e.g.
    portainer is ~0.05% of the tree). A newly-added service isn't in the priors, so it's never
    "expected" (no false page on an add); an intentional removal stops being expected after one cycle
    (it drops out of the prior window). Fewer than min_history+1 snapshots -> too little baseline, up.
    An empty latest listing -> skip (a genuine total loss is already caught by the size guard above).
    """
    if len(listings) < min_history + 1:
        return True, "presence guard skipped — only %d snapshot(s) of history" % len(
            listings
        )
    latest = listings[-1]
    if not latest:
        return True, "presence guard skipped — latest listing empty"
    priors = listings[-(min_history + 1) : -1]

    def present(m, name):
        return m.get(name, 0) > 0

    expected = {n for n in priors[0] if all(present(p, n) for p in priors)}
    missing = sorted(n for n in expected if not present(latest, n))
    if missing:
        return False, (
            "%d service dir(s) vanished from the latest backup (present in the prior %d "
            "snapshots): %s — a kopiaignore over-match or dropped bind mount?"
            % (len(missing), min_history, ", ".join(missing))
        )
    return True, "all %d expected service dir(s) present" % len(expected)


# --- checks: each returns (ok, msg) -----------------------------------------


def _kopia_dir_files(root_id):
    """{top_level_dir_name: recursive_file_count} for a Kopia snapshot's root object (dirs only).

    Fetches /api/v1/objects/<rootID>, whose `entries` list carries each child's type ('d'/'f') and,
    for directories, a recursive `summ.files`. Feeds check_backup's presence guard.
    """
    if not root_id:
        return {}
    data = _get_json(KOPIA_URL + "/api/v1/objects/" + root_id)
    return {
        e.get("name"): int((e.get("summ") or {}).get("files", 0))
        for e in (data.get("entries") or [])
        if e.get("type") == "d"
    }


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
    # Freshness + errorCount are clean — now guard against a silent SHRINK (a dropped bind mount or a
    # kopiaignore over-match that the fresh/0-error snapshot above happily hides). Pull the source's
    # snapshot history and floor the latest against the trailing median.
    src = backup_source(data, BACKUP_PATH)
    q = urllib.parse.urlencode(
        {
            "userName": src.get("userName", ""),
            "host": src.get("host", ""),
            "path": BACKUP_PATH,
        }
    )
    snaps = _get_json(KOPIA_URL + "/api/v1/snapshots?" + q).get("snapshots", [])
    size_ok, size_msg = backup_size_regression(
        snaps, BACKUP_SIZE_DROP_PCT, BACKUP_SIZE_MIN_HISTORY
    )
    if not size_ok:
        return False, size_msg
    # Presence guard: the aggregate size floor can't see a WHOLE small service dir vanish (portainer
    # is ~0.05% of the tree, far under the 20% floor). Fetch the top-level dir listing of the latest +
    # trailing snapshots and page if a consistently-backed-up service dir left the latest.
    recent = sorted(snaps, key=lambda s: s.get("endTime") or s.get("startTime") or "")[
        -(BACKUP_SIZE_MIN_HISTORY + 1) :
    ]
    listings = [_kopia_dir_files(s.get("rootID")) for s in recent]
    pres_ok, pres_msg = backup_presence_regression(listings, BACKUP_SIZE_MIN_HISTORY)
    if not pres_ok:
        return False, pres_msg
    return True, "last snapshot %.1fh ago, 0 errors; %s; %s" % (
        age_h,
        size_msg,
        pres_msg,
    )


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
        'changes(container_start_time_seconds{name!=""}[%s])' % RESTART_WINDOW
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
        'sum(increase(container_oom_events_total{name!=""}[%s])) by (name)' % OOM_WINDOW
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
    ratio_vec = prom_vector(
        'sum(rate(container_cpu_cfs_throttled_periods_total{name!=""}[%s])) by (name) '
        '/ sum(rate(container_cpu_cfs_periods_total{name!=""}[%s])) by (name)'
        % (CPU_WINDOW, CPU_WINDOW)
    )
    lost_cores = dict(
        (m.get("name", "?"), v)
        for m, v in prom_vector(
            'sum(rate(container_cpu_cfs_throttled_seconds_total{name!=""}[%s])) by (name)'
            % CPU_WINDOW
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
        if (
            dt.tzinfo is None
        ):  # n8n normally emits UTC 'Z'; assume UTC if a naive ts slips through
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


def gitops_status(hold_sha, diverged_sha=None):
    """Pure: is the deploy pipeline in a state needing operator action? Returns (ok, msg).

    Two down states share this monitor: a rolled-back commit HELD pending a revert, and a
    local↔origin DIVERGENCE where the deployer can't fast-forward and silently noops forever while
    origin's new commits never deploy (both other GitOps signals stay green — 2026-07-15 review L3).
    """
    if hold_sha:
        return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
    if diverged_sha:
        return False, (
            "local diverged from origin at %s — deployer can't fast-forward, new commits "
            "aren't deploying; reconcile the host tree" % diverged_sha[:8]
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
    """Failed executions of active ("Prod") n8n workflows within N8N_FAIL_WINDOW.

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia).
    Empty N8N_API_KEY -> disabled (stays up) so it never false-pages before the operator
    sets the key. An unreachable/erroring API raises -> the loop renders it down with the
    error, like check_targets_down (a dead API surfaces, not silent-green).
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
    offenders = n8n_failures(workflows, executions, parse_duration(N8N_FAIL_WINDOW))
    total = sum(c for _, c in offenders)
    if total > N8N_FAIL_MAX:
        desc = ", ".join("%s (%d)" % (sanitize(n), c) for n, c in offenders[:5])
        return False, "%d active workflow(s) failed in %s: %s" % (
            len(offenders),
            N8N_FAIL_WINDOW,
            desc,
        )
    return True, "no active-workflow failures in %s" % N8N_FAIL_WINDOW


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
        _read_gitops_marker("hold_sha"), _read_gitops_marker("diverged_sha")
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
      - ALL arms absent -> HA's whole Prometheus scrape is down (Scrape Targets' page).
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
    _ups_down_streak += 1
    if _ups_down_streak < UPS_CONSECUTIVE:
        return True, "down streak %d/%d (grace): %s" % (
            _ups_down_streak,
            UPS_CONSECUTIVE,
            msg,
        )
    return False, "%s (%d cycles)" % (msg, _ups_down_streak)


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


def restore_drill(state, age_s, max_age_s):
    if not state.get("ok"):
        return False, "last restore drill FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last successful restore drill %.1fd ago (max %dd)" % (
            age_s / 86400,
            max_age_s / 86400,
        )
    return True, "restore drill ok %.1fd ago: %s" % (
        age_s / 86400,
        state.get("msg", ""),
    )


def check_restore_drill():
    try:
        with open(RESTORE_DRILL_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no restore-drill state (drill never ran?)"
    except ValueError, TypeError:
        return False, "restore-drill state unparseable"
    return restore_drill(state, age_s, RESTORE_DRILL_MAX_AGE_S)


def verify(state, age_s, max_age_s):
    """Pure: did the last weekly `kopia snapshot verify` pass, and recently? (ok, msg).

    Same state-file idiom as restore_drill/b2_usage. The verify proves stored blobs are
    READABLE across all snapshots (the restore drill proves one service's tree restores);
    a failure here is detected B2 bit-rot / repo corruption — the weakest link in the
    single offsite copy's integrity chain, and previously un-alerted.
    """
    if not state.get("ok"):
        return False, "last snapshot verify FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last successful verify %.1fd ago (max %dd)" % (
            age_s / 86400,
            max_age_s / 86400,
        )
    return True, "verify ok %.1fd ago: %s" % (age_s / 86400, state.get("msg", ""))


def check_verify():
    try:
        with open(VERIFY_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no verify state (verify never ran?)"
    except ValueError, TypeError:
        return False, "verify state unparseable"
    return verify(state, age_s, VERIFY_MAX_AGE_S)


def content_verify(state, age_s, max_age_s):
    """Pure: did the last quarterly deep (25%) content verify pass, and recently enough? (ok, msg).

    Same state-file idiom as verify (the weekly 1% pass). This re-reads a QUARTER of every snapshot's
    file content, so it catches a mis-purge / blob-accounting bug the 1% sample can miss; a failure is
    detected B2 bit-rot / repo corruption on the deep sample.
    """
    if not state.get("ok"):
        return False, "last deep content verify FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last successful content verify %.0fd ago (max %.0fd)" % (
            age_s / 86400,
            max_age_s / 86400,
        )
    return True, "content verify ok %.0fd ago: %s" % (
        age_s / 86400,
        state.get("msg", ""),
    )


def check_content_verify():
    try:
        with open(CONTENT_VERIFY_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no content-verify state (verify never ran?)"
    except ValueError, TypeError:
        return False, "content-verify state unparseable"
    return content_verify(state, age_s, CONTENT_VERIFY_MAX_AGE_S)


def pi_peers(state, age_s, max_age_s):
    """Pure: did the last wg-easy Pi-peer backup pull succeed, and recently? (ok, msg).

    Same state-file idiom as verify/restore_drill. The pull is the only path that carries the Pi's
    un-rebuildable WireGuard peer keys into Kopia scope; because it never --deletes, a silently
    failing pull leaves stale-but-present files that keep Backup Freshness green — so a FAILED or
    STALE pull is the signal that the offsite copy of those keys has quietly stopped refreshing.
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
    try:
        with open(PI_PEERS_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no Pi-peer backup state (pull never ran?)"
    except ValueError, TypeError:
        return False, "Pi-peer backup state unparseable"
    return pi_peers(state, age_s, PI_PEERS_MAX_AGE_S)


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
    try:
        with open(DISK_PRUNE_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no disk-autoprune state (never ran?)"
    except ValueError, TypeError:
        return False, "disk-autoprune state unparseable"
    return disk_prune(state, age_s, DISK_PRUNE_MAX_AGE_S)


def home_allowlist(state, age_s, max_age_s):
    """Pure: did the last CrowdSec home-IP allowlist update run succeed, and recently? (ok, msg).

    Same state-file idiom as pi_peers/verify. The updater writes state on EVERY run (incl. the
    IP-unchanged fast path), so a stale timestamp means the every-5-min cron stopped running, and
    ok=false means ipify was unreachable or a cscli call errored — either way the home path may start
    tripping the WAF on the next IP rotation with no other signal.
    """
    if not state.get("ok"):
        return False, "last home-allowlist update FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last home-allowlist update %.0f min ago (max %.0f)" % (
            age_s / 60,
            max_age_s / 60,
        )
    return True, "home-allowlist ok %.0f min ago: %s" % (
        age_s / 60,
        state.get("msg", ""),
    )


def check_home_allowlist():
    try:
        with open(HOME_ALLOWLIST_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no home-allowlist state (updater never ran?)"
    except ValueError, TypeError:
        return False, "home-allowlist state unparseable"
    return home_allowlist(state, age_s, HOME_ALLOWLIST_MAX_AGE_S)


def docker_user(state, age_s, max_age_s):
    """Pure: is the DOCKER-USER origin lock currently applied, per the last live-chain verify? (ok, msg).

    Same state-file idiom as home_allowlist/verify. The verify cron re-reads the live iptables chain
    every run, so ok=false means the terminal DROP or the RETURN allows went missing (origin reachable
    direct), and a stale timestamp means the verify cron itself stopped.
    """
    if not state.get("ok"):
        return False, "DOCKER-USER origin lock NOT applied: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return (
            False,
            "last DOCKER-USER verify %.0f min ago (max %.0f) — verify cron stopped?"
            % (age_s / 60, max_age_s / 60),
        )
    return True, "origin lock verified %.0f min ago: %s" % (
        age_s / 60,
        state.get("msg", ""),
    )


def check_docker_user():
    try:
        with open(DOCKER_USER_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no DOCKER-USER verify state (verify never ran?)"
    except ValueError, TypeError:
        return False, "DOCKER-USER verify state unparseable"
    return docker_user(state, age_s, DOCKER_USER_MAX_AGE_S)


def cloudflare_drift(state, age_s, max_age_s):
    """Pure: did the last Cloudflare-IP drift check pass, and recently? (ok, msg).

    Same state-file idiom as home_allowlist/docker_user. The weekly cron writes state on every run;
    ok=false means the hardcoded cloudflare_ips allowlist no longer matches Cloudflare's published
    ranges (or the fetch failed) — a stale list silently DROPs a client on a new CF range at the
    DOCKER-USER origin lock. A stale timestamp means the weekly cron stopped.
    """
    if not state.get("ok"):
        return False, "Cloudflare IP allowlist drift: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return (
            False,
            "last Cloudflare-IP drift check %.1fd ago (max %.1fd) — verify cron stopped?"
            % (age_s / 86400, max_age_s / 86400),
        )
    return True, "cloudflare_ips ok %.1fd ago: %s" % (
        age_s / 86400,
        state.get("msg", ""),
    )


def check_cloudflare_drift():
    try:
        with open(CLOUDFLARE_DRIFT_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no Cloudflare-drift state (check never ran?)"
    except ValueError, TypeError:
        return False, "Cloudflare-drift state unparseable"
    return cloudflare_drift(state, age_s, CLOUDFLARE_DRIFT_MAX_AGE_S)


def appsec(state, age_s, max_age_s):
    """Pure: is the CrowdSec AppSec inline WAF loaded and enforcing, per the last verify? (ok, msg).

    Same state-file idiom as home_allowlist/docker_user/cloudflare_drift. The verify cron asserts the
    live crowdsec agent has its appsec config + inband rulesets loaded every run. The bouncer fails
    OPEN (crowdsecAppsecUnreachableBlock:false), so a broken appsec engine — a bad `cscli collections
    upgrade`, a hub rename dropping appsec-virtual-patching/appsec-generic-rules — silently degrades
    the edge to ban-list-only while the container stays up + `cscli lapi status`-healthy, which Scrape
    Targets can't catch (it only sees a TOTAL crowdsec death). ok=false means the live assert failed
    (WAF not enforcing); a stale timestamp means the verify cron itself stopped.
    """
    if not state.get("ok"):
        return False, "CrowdSec AppSec WAF not enforcing: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return (
            False,
            "last AppSec verify %.0f min ago (max %.0f) — verify cron stopped?"
            % (age_s / 60, max_age_s / 60),
        )
    return True, "AppSec WAF enforcing, verified %.0f min ago: %s" % (
        age_s / 60,
        state.get("msg", ""),
    )


def check_appsec():
    try:
        with open(APPSEC_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no AppSec verify state (verify never ran?)"
    except ValueError, TypeError:
        return False, "AppSec verify state unparseable"
    return appsec(state, age_s, APPSEC_MAX_AGE_S)


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
            age_s / 86400,
            max_age_s / 86400,
        )
    try:
        used = float(state["bytes"])
    except KeyError, TypeError, ValueError:
        return False, "B2 usage state missing/invalid bytes"
    pct = used / cap_bytes * 100
    msg = "B2 %.2f/%.0fGB billable (%.0f%% of plan)" % (
        used / 1e9,
        cap_bytes / 1e9,
        pct,
    )
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
    except ValueError, TypeError:
        return False, "B2-usage state unparseable"
    return b2_usage(state, age_s, B2_USAGE_MAX_AGE_S, B2_CAP_BYTES, B2_USAGE_MAX_PCT)


def b2_trend(current, predicted, cap_bytes, horizon_d, age_s=None, max_age_s=None):
    """Pure: project B2 billable bytes forward and decide if the cap is imminent. (ok, msg).

    `current` = the gauge now; `predicted` = predict_linear(metric[window], horizon) — the
    fitted value horizon_d days out. Either being None (gauge absent/never scraped) -> down
    (fail-stale). `age_s` (the textfile's mtime age, None to skip the guard) -> down when older
    than `max_age_s`: node-exporter keeps serving a frozen gauge after a failed .prom write, so
    without this a stuck textfile reads flat -> false ok. A flat or falling trend
    (predicted <= current) -> ok. Otherwise derive the per-day slope from the same projection:
    `down` when the cap is reached within the horizon (predicted >= cap), naming the runway; ok
    with the runway noted when it's further out.
    """
    if current is None or predicted is None:
        return False, "B2 trend metric unavailable (%s not exported?)" % B2_TREND_METRIC
    if age_s is not None and max_age_s is not None and age_s > max_age_s:
        return False, (
            "B2 trend gauge STALE: %s textfile %.1fd old (max %.1fd) — cron wrote state.json "
            "but the .prom export is frozen, so the trend is blind"
            % (
                B2_TREND_METRIC,
                age_s / 86400,
                max_age_s / 86400,
            )
        )
    cur_gb, cap_gb = current / 1e9, cap_bytes / 1e9
    if predicted <= current:
        return True, "B2 flat/shrinking (%.2f/%.0fGB, %.0fd projection %.2fGB)" % (
            cur_gb,
            cap_gb,
            horizon_d,
            predicted / 1e9,
        )
    per_day_gb = (predicted - current) / horizon_d / 1e9
    days_to_cap = (cap_bytes - current) / ((predicted - current) / horizon_d)
    if predicted >= cap_bytes:
        return False, (
            "B2 on track to hit the %.0fGB cap in ~%.0fd (now %.2fGB, +%.2fGB/day) "
            "— prune/upgrade before snapshots fail"
            % (cap_gb, days_to_cap, cur_gb, per_day_gb)
        )
    return True, "B2 %.2f/%.0fGB, +%.2fGB/day, cap ~%.0fd out (> %.0fd horizon)" % (
        cur_gb,
        cap_gb,
        per_day_gb,
        days_to_cap,
        horizon_d,
    )


def check_b2_trend():
    """Project the kopia_b2_billable_bytes gauge to warn of a filling bucket before the 85% cap.

    Prom-dependent (suppressed under the Prometheus-reachability gate). The gauge is exported
    by the daily b2-usage host cron via the node-exporter textfile collector; node-exporter
    serves the last written value continuously, so predict_linear has a dense series even
    between the cron's daily writes (a stalled cron reads flat — its own staleness is the
    B2_USAGE check's job, off the state.json). The textfile-mtime guard catches the narrower
    case where the cron runs but ONLY its .prom write fails: state.json stays fresh (B2_USAGE
    green) while the gauge freezes here.
    """
    current = prom_scalar(B2_TREND_METRIC)
    predicted = prom_scalar(
        "predict_linear(%s[%s], %d)"
        % (B2_TREND_METRIC, B2_TREND_WINDOW, int(B2_TREND_HORIZON_D * 86400))
    )
    mtime = prom_scalar(B2_TREND_MTIME_QUERY)
    age_s = (time.time() - mtime) if mtime is not None else None
    return b2_trend(
        current, predicted, B2_CAP_BYTES, B2_TREND_HORIZON_D, age_s, B2_TREND_MAX_AGE_S
    )


def maintenance(state, age_s, max_age_s):
    """Pure: is kopia FULL maintenance healthy, and the check recent? (ok, msg).

    Same state-file idiom as verify/b2_usage. The host cron decides `ok` from `kopia maintenance
    info --json` (full enabled, owner set, next full run not overdue, newest run succeeded); here
    we add staleness. Full maintenance GCs expired blobs from B2, so a stall is the upstream CAUSE
    the b2_usage check only catches later as a downstream symptom (and B2 headroom is thin).
    """
    if not state.get("ok"):
        return False, "kopia full maintenance UNHEALTHY: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "maintenance check %.1fd old (max %.1fd)" % (
            age_s / 86400,
            max_age_s / 86400,
        )
    return True, "maintenance ok %.1fd ago: %s" % (age_s / 86400, state.get("msg", ""))


def check_maintenance():
    try:
        with open(MAINTENANCE_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no maintenance state (check never ran?)"
    except ValueError, TypeError:
        return False, "maintenance state unparseable"
    return maintenance(state, age_s, MAINTENANCE_MAX_AGE_S)


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
    _ha_down_streak += 1
    if _ha_down_streak < HA_CONSECUTIVE:
        return True, "down streak %d/%d (deploy/restart grace): %s" % (
            _ha_down_streak,
            HA_CONSECUTIVE,
            msg,
        )
    return False, "%s (%d cycles)" % (msg, _ha_down_streak)


def loki_count(selector, window):
    """Instant LogQL query: total log lines for `selector` over `window`. None if no series.

    Loki's instant-query endpoint evaluates a metric query — here
    sum(count_over_time(SELECTOR[WINDOW])) — and returns a vector with the same
    [ts, value] shape prom_scalar parses, so we read result[0].value[1].
    """
    query = "sum(count_over_time(%s[%s]))" % (selector, window)
    url = LOKI_URL + "/loki/api/v1/query?" + urllib.parse.urlencode({"query": query})
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError("loki query status=%s" % data.get("status"))
    result = data.get("data", {}).get("result", [])
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
    _discord_down_streak += 1
    if _discord_down_streak < DISCORD_CONSECUTIVE:
        return True, "down streak %d/%d (transient grace): %s" % (
            _discord_down_streak,
            DISCORD_CONSECUTIVE,
            msg,
        )
    return False, "%s (%d cycles)" % (msg, _discord_down_streak)


def recyclarr_sync_ok(failed_count, succeeded_count, window):
    """Decide recyclarr sync health from supercronic's structured job lines over `window`.

    Counts of `job failed` / `job succeeded` log lines (None = no matching Loki series -> 0):
      - any `job failed`  -> a sync exited non-zero (the silent v8-breakage class).
      - zero `job succeeded` -> no successful run in the window: the scheduler stalled or every
        run is failing (the container healthcheck only proves supercronic itself is alive).
    """
    failed = int(failed_count or 0)
    succeeded = int(succeeded_count or 0)
    if failed:
        return (
            False,
            "recyclarr sync failed %d time(s) in %s (see `docker logs recyclarr`)"
            % (
                failed,
                window,
            ),
        )
    if succeeded == 0:
        return (
            False,
            "no successful recyclarr sync in %s — scheduler stalled or sync failing"
            % window,
        )
    return True, "recyclarr sync ok (%d succeeded in %s)" % (succeeded, window)


def check_recyclarr():
    """Loki-based recyclarr sync watchdog (the healthcheck only watches supercronic, not sync)."""
    failed = loki_count(
        "%s |= `job failed`" % RECYCLARR_LOKI_SELECTOR, RECYCLARR_WINDOW
    )
    succeeded = loki_count(
        "%s |= `job succeeded`" % RECYCLARR_LOKI_SELECTOR, RECYCLARR_WINDOW
    )
    return recyclarr_sync_ok(failed, succeeded, RECYCLARR_WINDOW)


def janitorr_errors_ok(count, uptime_s, window_s, grace_s):
    """Pure: decide janitorr scheduled-task health from the post-startup error count. (ok, msg).

    See the JANITORR_* config block for why this gates on Prometheus uptime instead of filtering
    the (generic, un-discriminable) ERROR line by content:
      - uptime None (metric absent: janitorr stopped/redeployed) -> ok (a stopped janitorr is
        Container Restarts'/Scrape Targets' concern, not this check's);
      - within grace_s of startup -> ok (the documented boot-race window);
      - otherwise `down` on any scheduled-task error in the post-startup window.
    """
    if uptime_s is None:
        return True, "janitorr uptime unknown (metric absent) — error check skipped"
    if uptime_s <= grace_s:
        return True, "startup grace — up %.0fs (<= %.0fs)" % (uptime_s, grace_s)
    n = int(count or 0)
    if n:
        return (
            False,
            "%d janitorr scheduled-task error(s) in the last %.0fm — see `docker logs janitorr`"
            % (n, window_s / 60.0),
        )
    return True, "no janitorr errors (up %.1fh)" % (uptime_s / 3600.0)


def check_janitorr():
    """Loki+Prometheus janitorr cleanup-error watchdog (see janitorr_errors_ok / the JANITORR_*
    config block). Prom-dependent (uptime gate) AND Loki-dependent (error count)."""
    uptime_s = prom_scalar(
        'time() - max(container_start_time_seconds{name="janitorr"})'
    )
    window_s = parse_duration(JANITORR_WINDOW)
    grace_s = JANITORR_STARTUP_GRACE_S
    count, eff_window_s = None, window_s
    if uptime_s is not None and uptime_s > grace_s:
        # Count only over the post-startup slice, so the documented boot race (all within the
        # first ~minute) can never be in-window once we're past grace.
        eff_window_s = min(window_s, uptime_s - grace_s)
        count = loki_count(
            "%s |= `%s`" % (JANITORR_LOKI_SELECTOR, JANITORR_ERROR_MATCH),
            "%ds" % int(eff_window_s),
        )
    return janitorr_errors_ok(count, uptime_s, eff_window_s, grace_s)


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
    ("arr_queue", _env("KUMA_PUSH_ARR_QUEUE", ""), check_arr_queue),
    (
        "prowlarr_indexers",
        _env("KUMA_PUSH_PROWLARR_INDEXERS", ""),
        check_prowlarr_indexers,
    ),
    ("gitops_alive", _env("KUMA_PUSH_GITOPS_ALIVE", ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
    ("restore_drill", _env("KUMA_PUSH_RESTORE_DRILL", ""), check_restore_drill),
    ("verify", _env("KUMA_PUSH_VERIFY", ""), check_verify),
    ("content_verify", _env("KUMA_PUSH_CONTENT_VERIFY", ""), check_content_verify),
    ("pi_peers", _env("KUMA_PUSH_PI_PEERS", ""), check_pi_peers),
    ("disk_prune", _env("KUMA_PUSH_DISK_PRUNE", ""), check_disk_prune),
    (
        "home_allowlist",
        _env("KUMA_PUSH_HOME_ALLOWLIST", ""),
        check_home_allowlist,
    ),
    ("docker_user", _env("KUMA_PUSH_DOCKER_USER", ""), check_docker_user),
    (
        "cloudflare_drift",
        _env("KUMA_PUSH_CLOUDFLARE_DRIFT", ""),
        check_cloudflare_drift,
    ),
    ("appsec", _env("KUMA_PUSH_APPSEC", ""), check_appsec),
    ("maintenance", _env("KUMA_PUSH_MAINTENANCE", ""), check_maintenance),
    ("b2_usage", _env("KUMA_PUSH_B2", ""), check_b2_usage),
    ("b2_trend", _env("KUMA_PUSH_B2_TREND", ""), check_b2_trend),
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
    ("recyclarr", _env("KUMA_PUSH_RECYCLARR", ""), check_recyclarr),
    ("janitorr", _env("KUMA_PUSH_JANITORR", ""), check_janitorr),
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
        "b2_trend",
        "ups",  # queries HA's Prometheus-scraped UPS battery sensors
        "janitorr",  # reads container_start_time_seconds for its startup-race uptime gate
        "promtail_dropped",  # increase(promtail_dropped_entries_total) instant query
    }
)

# One level BELOW the Prometheus gate: a single exporter down while Prometheus is UP fails every
# check reading its metrics at once. node-exporter death false-pages Root Disk + Memory + B2 Usage
# Trend (node_* / the kopia_b2 textfile gauge go unavailable -> down) on top of the legitimate Scrape
# Targets page; cadvisor death makes restarts/oom/cpu read an empty vector -> silently green. Scrape
# Targets already names the dead `up{job=...}==0`, so run_once suppresses each dead exporter's
# dependents (pushes `up` with a skip msg, heartbeat kept alive) and lets Scrape Targets be the single
# page — the same one-root-cause-one-alert shape as the Prometheus gate, keyed by the Prometheus `job`
# label. Guarded by a test against CHECKS. (`cert`/`traefik5xx` read Traefik's own metrics, not these
# two exporters, so they're not mapped here.)
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory", "b2_trend"}),
    "cadvisor": frozenset({"restarts", "oom", "cpu"}),
}

# Loki-reachability gate — the peer of the Prometheus gate for the Loki-querying checks. A single
# Loki outage makes loki_count raise in ALL of them at once (Loki Log Ingestion + Recyclarr Sync +
# Janitorr Errors) -> a 3-monitor storm for one root cause. run_once probes Loki first
# (check_loki_reachable -> its own "Loki Reachable" monitor) and, when it's unreachable, SUPPRESSES
# these (pushes `up` with a skip msg so their push heartbeats stay alive) so only Loki Reachable
# pages. Loki being UP but promtail not shipping is a different signal Loki Log Ingestion still
# surfaces (it evaluates whenever Loki is reachable). Guarded by a test against CHECKS.
LOKI_DEPENDENT = frozenset({"loki_ingestion", "recyclarr", "janitorr"})

# Reach-out checks that poll a live app dependency (kopia/n8n/sonarr/radarr/prowlarr/scrutiny/the Pi
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
    {"backup", "n8n", "arr_queue", "pi_pressure", "prowlarr_indexers", "scrutiny"}
)

_grace_streaks = {}


def apply_startup_grace(name, ok, msg, threshold, streaks):
    """Pure: hold a reach-out check `up` through the first `threshold`-1 consecutive down cycles.

    `streaks` is a name->consecutive-down-count dict, mutated in place (like the module-global
    streak counters check_ha_heartbeat/check_cpu_throttle keep). An `ok` result resets the count;
    the `threshold`'th straight down passes through unchanged. Same "down streak n/N" held-up /
    "(n cycles)" paging msg shape as the HA/Discord grace, so a held cycle stays legible in the log.
    """
    if ok:
        streaks[name] = 0
        return ok, msg
    n = streaks.get(name, 0) + 1
    streaks[name] = n
    if n < threshold:
        return True, "down streak %d/%d (startup/redeploy grace): %s" % (
            n,
            threshold,
            msg,
        )
    return False, "%s (%d cycles)" % (msg, n)


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
    prom_ok, prom_msg = _evaluate("prometheus", check_prometheus)
    log("OK  " if prom_ok else "DOWN", "prometheus", "-", prom_msg)
    push(_env("KUMA_PUSH_PROMETHEUS", ""), prom_ok, prom_msg)

    # Exporter-reachability gate (one level below the Prometheus gate): when Prometheus is up, probe
    # `up` once and suppress each dead exporter's dependents so a node-exporter/cadvisor death is one
    # page (Scrape Targets), not a 3-monitor false-page storm / silent-green split. A failure to
    # DETERMINE exporter health leaves `suppressed` empty (fail toward alerting, never masking).
    suppressed = set()
    if prom_ok:
        try:
            for job in down_exporters(prom_vector("up")):
                suppressed |= EXPORTER_DEPENDENT[job]
        except Exception as e:
            log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (LOKI_DEPENDENT).
    loki_ok, loki_msg = _evaluate("loki_reachable", check_loki_reachable)
    log("OK  " if loki_ok else "DOWN", "loki_reachable", "-", loki_msg)
    push(_env("KUMA_PUSH_LOKI_REACHABLE", ""), loki_ok, loki_msg)

    for name, token, fn in CHECKS:
        if not prom_ok and name in PROM_DEPENDENT:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            log("SKIP", name, "-", msg)
        elif not loki_ok and name in LOKI_DEPENDENT:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
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
    log("monitor-bridge starting (interval=%ss, once=%s)" % (INTERVAL, once))
    while True:
        run_once()
        touch_heartbeat()
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
