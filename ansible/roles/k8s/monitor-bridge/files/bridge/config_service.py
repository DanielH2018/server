"""The app-facing half of monitor-bridge's configuration.

Traefik, n8n, the *arr stack, the deployer's own state files, the etcd restore drill, the
staging-gate backfill and Home Assistant — every threshold read by a check in
`checks/service.py`, plus the Traefik latency and error-rate bounds `checks/cluster.py` reads.

Field justifications sit beside the declarations, env var names and defaults beside the reads.
Composed into `Config` by `bridge/config.py`; imports nothing from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceConfig:
    """Traefik, n8n, the *arr stack, the deployer state files, the drills and Home Assistant."""

    TRAEFIK_5XX_PCT: float
    TRAEFIK_404_PCT: float
    TRAEFIK_MIN_RPS: float
    TRAEFIK_SLOW_BUCKET: str
    TRAEFIK_SLOW_PCT: float
    N8N_URL: str
    N8N_API_KEY: str = field(repr=False)
    N8N_FAIL_WINDOW: str
    N8N_CONSECUTIVE_MAX: int
    N8N_SYSTEMIC_STREAK: int
    N8N_SYSTEMIC_MAX: int
    SONARR_URL: str
    SONARR_API_KEY: str = field(repr=False)
    RADARR_URL: str
    RADARR_API_KEY: str = field(repr=False)
    BAZARR_URL: str
    BAZARR_API_KEY: str = field(repr=False)
    PROWLARR_URL: str
    PROWLARR_API_KEY: str = field(repr=False)
    PROWLARR_INDEXER_MIN_DOWN_MIN: float
    PROWLARR_INDEXER_IGNORE: str
    GITOPS_STATE_DIR: str
    GITOPS_MAX_AGE_S: float
    ETCD_DRILL_STATE_DIR: str
    ETCD_DRILL_MAX_AGE_S: float
    STAGING_BACKFILL_MAX_AGE_S: float
    GITOPS_BEHIND_MAX_S: float
    HA_URL: str
    HA_TOKEN: str = field(repr=False)
    HA_HEARTBEAT_MAX_AGE_S: float
    HA_HEARTBEAT_ENTITY: str
    HA_CONSECUTIVE: int
    HA_BAN_WINDOW: str
    HA_BAN_SELECTOR: str


def service_config(
    _env: Callable[..., str],
    _int: Callable[[str, str], int],
    _num: Callable[[str, str], float],
    _env_file: Callable[..., str],
) -> ServiceConfig:
    """The app fields, read through the parsers `load_config` built over its environment."""
    return ServiceConfig(
        TRAEFIK_5XX_PCT=_num("TRAEFIK_5XX_PCT", "5"),
        # The 404 SHARE of entrypoint traffic that means the edge has lost its routers.
        # High, not low, on purpose: a homelab edge serves a steady trickle of ordinary
        # 404s (favicons, probes, a stale bookmark), measured at 4.0% of 0.83 rps on
        # 2026-09-06, while the total-404 outage that day was 100% of 0.61 rps. 90 sits in
        # that gap with a wide margin on both sides.
        TRAEFIK_404_PCT=_num("TRAEFIK_404_PCT", "90"),
        TRAEFIK_MIN_RPS=_num("TRAEFIK_MIN_RPS", "0.05"),
        # Slowness is measured at a histogram BUCKET BOUNDARY, not with histogram_quantile.
        # Traefik's default buckets are 0.1 / 0.3 / 1.2 / 5.0 / +Inf, so between 1.2s and 5.0s
        # there is nothing to interpolate from and a quantile landing there is invented, not
        # measured. The old check compared histogram_quantile(0.95, ...) against 3s — a
        # threshold sitting inside that empty 3.8s-wide gap. Measured on homepage@docker
        # 2026-08-06 13:15-13:20 UTC: Prometheus reported p95 4.058s while the Traefik access
        # log for the same window showed a real p95 of 1.576s (114 requests, 24 over 1.2s, max
        # 3.063s). Every firing was that arithmetic. Across all 42,598 homepage requests that
        # day only 8 exceeded 3s, never more than 2 in a 5-minute window, so no window's real
        # p95 came near it.
        #
        # The bucket counts themselves are exact, so "more than 5% of requests exceeded 5.0s"
        # IS "p95 above 5.0s", stated without interpolation. Keep TRAEFIK_SLOW_BUCKET on a
        # boundary Traefik actually emits — an le= that matches no series selects nothing (see
        # the unmeasurable branch in the verdict).
        TRAEFIK_SLOW_BUCKET=_env("TRAEFIK_SLOW_BUCKET", "5.0"),
        TRAEFIK_SLOW_PCT=_num("TRAEFIK_SLOW_PCT", "5"),
        N8N_URL=_env("N8N_URL", "http://n8n:5678").rstrip("/"),
        N8N_API_KEY=_env("N8N_API_KEY", ""),
        # n8n hides successful executions (EXECUTIONS_DATA_SAVE_ON_SUCCESS=none, kept that way
        # to bound database.sqlite + its B2 backup churn), so "consecutive" can't be read from
        # one snapshot — the per-workflow failure streak is accumulated across cycles in
        # _n8n_streaks (see n8n_update_streaks): it advances once per NEW error (deduped by
        # execution id) and resets when a workflow's latest error ages past N8N_FAIL_WINDOW
        # (recovered / went idle).
        N8N_FAIL_WINDOW=_env("N8N_FAIL_WINDOW", "2h"),
        N8N_CONSECUTIVE_MAX=_int("N8N_CONSECUTIVE_MAX", "3"),
        # Systemic catch: if N8N_SYSTEMIC_MAX+ workflows are each failing >=
        # N8N_SYSTEMIC_STREAK times, something is wrong with n8n itself — page now as ONE alert
        # instead of waiting for each to reach N8N_CONSECUTIVE_MAX (and instead of a
        # per-workflow flood).
        N8N_SYSTEMIC_STREAK=_int("N8N_SYSTEMIC_STREAK", "2"),
        N8N_SYSTEMIC_MAX=_int("N8N_SYSTEMIC_MAX", "2"),
        # Sonarr/Radarr queue warnings: the 2026-07-01 incident — an indexer served a poisoned
        # fake-episode .exe, sonarr itself blocked the import and flagged the queue item
        # trackedDownloadStatus "warning" (message: "Caution: Found executable file with
        # extension: '.exe'") — but nothing paged, so the release sat seeding for a full day
        # before a manual review caught it. Polled directly (X-Api-Key header), same "internal
        # REST API, empty key disables" idiom as N8N_API_KEY.
        SONARR_URL=_env("SONARR_URL", "http://sonarr:8989").rstrip("/"),
        SONARR_API_KEY=_env("SONARR_API_KEY", ""),
        RADARR_URL=_env("RADARR_URL", "http://radarr:7878").rstrip("/"),
        RADARR_API_KEY=_env("RADARR_API_KEY", ""),
        # Bazarr's link to Sonarr and Radarr. Bazarr holds its OWN copies of their API keys, in
        # its config on the bazarr-config PVC and entered through its UI — so no Ansible
        # template carries them and no deploy updates them. On 2026-08-29 a rotation swept the
        # eight templated consumers, missed Bazarr, and its SignalR client dropped into a
        # reconnect loop that leaked 173 MiB to its 1Gi cap in 90 minutes and OOM-killed it. The
        # ONLY signal was the "k3s Container OOM" tile, which clears one hour after the kill and
        # takes the evidence with it, leaving Bazarr fetching no subtitles, silently.
        #
        # NOTE the header spelling: `X-API-KEY`, not the `X-Api-Key` Sonarr and Radarr take.
        # Verified against the live app 2026-08-29 — a request with no key returns 401, so the
        # key is doing work.
        BAZARR_URL=_env("BAZARR_URL", "http://bazarr:6767").rstrip("/"),
        BAZARR_API_KEY=_env("BAZARR_API_KEY", ""),
        # Prowlarr sustained-indexer watchdog: Prowlarr's in-app health notification is binary —
        # with warnings on every indexer flap pages, with warnings off only the
        # all-indexers-down red error fires; there's no duration grace. We poll
        # /api/v1/indexerstatus and go `down` only when an indexer has been FAILING for >=
        # PROWLARR_INDEXER_MIN_DOWN_MIN (age from Prowlarr's own initialFailure, so it survives
        # a monitor-bridge redeploy), suppressing the sub-threshold flaps public trackers throw
        # that self-clear inside Prowlarr's ~5-15min backoff. Empty key = disabled (stays up),
        # same idiom as N8N_API_KEY. Already on `media`, so prowlarr:9696 is reachable.
        PROWLARR_URL=_env("PROWLARR_URL", "http://prowlarr:9696").rstrip("/"),
        PROWLARR_API_KEY=_env("PROWLARR_API_KEY", ""),
        PROWLARR_INDEXER_MIN_DOWN_MIN=_num("PROWLARR_INDEXER_MIN_DOWN_MIN", "30"),
        # Comma-separated indexer names (case-insensitive) never counted as offenders. For
        # chronically flaky PUBLIC trackers whose backend routinely 503s/times-out past the
        # sustained-down gate (e.g. The Pirate Bay's apibay.org) — they'd page every outage
        # though the other indexers cover the same searches. Prowlarr's own all-indexers-down
        # onHealthIssue is the backstop if every indexer, ignored or not, fails at once.
        # Empty = ignore nothing.
        PROWLARR_INDEXER_IGNORE=_env("PROWLARR_INDEXER_IGNORE", ""),
        # Since the Docker uninstall (2026-08-14) this reads daniel-box's own deployer state —
        # the pod is pinned to that node and hostPath-mounts /var/lib/gitops-deploy. One
        # deployer remains in the fleet (the Pi runs has_gitops: false), so one watcher.
        GITOPS_STATE_DIR=_env("GITOPS_STATE_DIR", "/gitops-state"),
        GITOPS_MAX_AGE_S=_num("GITOPS_MAX_AGE_MIN", "90") * 60,
        ETCD_DRILL_STATE_DIR=_env("ETCD_DRILL_STATE_DIR", "/etcd-drill-state"),
        # DECIDED: 8 days, DERIVED from the drill's cadence rather than picked round. The cron
        # is k3s_etcd_restore_drill_cron = "20 10 * * 1" — weekly, Monday 10:20 — so anything
        # over 7 days means a scheduled run did not happen or did not pass. 8 gives one day of
        # slack for the run itself and for a check that evaluates just before the window, and no
        # more: a wider grace and the monitor clears the very miss it exists to catch, which is
        # how a 24h grace against a 23h gap read green on 2026-08-25. Move this ONLY together
        # with the cron; the two are pinned to each other by
        # test_etcd_drill_grace_is_derived_from_the_cron.
        ETCD_DRILL_MAX_AGE_S=_num("ETCD_DRILL_MAX_AGE_DAYS", "8") * 86400,
        # The staging-gate backfill ratchet's run-recency window. It shares GITOPS_STATE_DIR:
        # the unit writes its heartbeat and Ansible writes its armed marker into
        # /var/lib/gitops-deploy, which this pod already hostPath-mounts for the gitops pair.
        #
        # DECIDED: 150 minutes, DERIVED from the timer rather than picked round.
        # staging-backfill.timer is OnUnitActiveSec=1h with RandomizedDelaySec=10min, and
        # TimeoutStartSec=25min bounds the run itself, so the worst-case gap between two
        # heartbeats is about 95 minutes. 150 clears that with slack and still falls short of
        # two cadences (190 min) — a window that spans two would tolerate a fully missed run,
        # which is the miss this check exists to catch. Move it only with the timer;
        # test_staging_backfill_window_is_derived_from_the_timer pins the pair.
        STAGING_BACKFILL_MAX_AGE_S=_num("STAGING_BACKFILL_MAX_AGE_MIN", "150") * 60,
        # How long the host may sit behind origin before GitOps Status pages. Generous on
        # purpose: the deployer ticks every 30 min, and the dirty-tree path (operator mid-edit)
        # is behind by design for as long as the edit lasts. 6 h pages a genuinely-stuck host
        # well inside a day while never firing on a normal push or a long editing session.
        GITOPS_BEHIND_MAX_S=_num("GITOPS_BEHIND_MAX_MIN", "360") * 60,
        # HA automation-engine heartbeat: an HA time_pattern automation stamps
        # input_datetime.ha_heartbeat with now() every minute, so its last_changed is fresh ONLY
        # while HA's automation scheduler is executing. We poll HA's /api/states over the apps
        # network (Bearer token) and go down when it's stale — catching a wedged-but-running HA
        # (HTTP :8123 up, scheduler stuck) that the container healthcheck can't see. Empty
        # URL/token = disabled (stays up), like N8N_API_KEY/PI_GLANCES_URL. 300s = 5 missed
        # 1-min beats; rides out an HA restart/deploy. Seconds (no unit suffix) — kept a plain
        # float here because parse_duration belongs to the verdict layer, not to config.
        HA_URL=_env("HA_URL", "").rstrip("/"),
        # File-mounted (HA_TOKEN_FILE) so this full-access HA long-lived token stays out of the
        # pod environment envFrom cannot filter; falls back to the HA_TOKEN env.
        HA_TOKEN=_env_file("HA_TOKEN", ""),
        HA_HEARTBEAT_MAX_AGE_S=_num("HA_HEARTBEAT_MAX_AGE", "300"),
        HA_HEARTBEAT_ENTITY="input_datetime.ha_heartbeat",
        # Consecutive-cycle hysteresis (like CPU_CONSECUTIVE) so a planned HA redeploy — which
        # takes the API unreachable for ~120s and then leaves the scheduler a beat behind —
        # doesn't page. 2 straight down cycles (~one full INTERVAL of continuous badness) before
        # `down`.
        HA_CONSECUTIVE=_int("HA_CONSECUTIVE", "2"),
        # ip_ban arm of the HA monitor. HA's ban middleware runs on every request and keys on
        # the peer address, so a burst of unauthenticated /api/ calls can ban an INFRASTRUCTURE
        # ip rather than an attacker — on 2026-08-23 five bad calls from the node's pod-network
        # gateway (10.42.0.1) banned it, and the probes then arriving from that IP got 403 and
        # crash-looped the pod. The probes no longer arrive from a bannable address (they exec
        # curl to 127.0.0.1), which fixes the crash loop but makes a ban SILENT: HA keeps
        # serving, while whatever shares that source IP stays locked out. This arm is the
        # visibility half.
        #
        # IT WATCHES THE BAN EVENT, NOT THE BAN STATE, and the difference matters when you read
        # it at 03:00. `Banned IP` is logged once, at ban time. The arm therefore pages for
        # HA_BAN_WINDOW after a ban is issued and then SELF-CLEARS, while the entry is still
        # sitting in /config/ip_bans.yaml. A ban that predates the window — or one reloaded from
        # that file by an HA restart, which logs nothing — is invisible here. So a green
        # ha_heartbeat does NOT mean "no IP is banned"; it means "no ban was issued in the last
        # HA_BAN_WINDOW".
        #
        # That is the only signal available: HA does not log its ongoing 403s to a banned peer,
        # and this pod cannot read HA's PVC. The DURABLE artifact is the Discord notification
        # Kuma fires on the down transition (push monitors run max_retries=0, so the push flips
        # state and notifies immediately) — not the monitor's colour, which is transient by
        # construction. When one fires, check /config/ip_bans.yaml by hand; do not wait for the
        # monitor to go green.
        HA_BAN_WINDOW=_env("HA_BAN_WINDOW", "1h"),
        # `container=`, NOT `app=`. Promtail's k8s stream carries
        # container/pod/job/machine/namespace/service_name/stream — there is no `app` label, so
        # `app="home-assistant"` matched no stream and the arm reported "no ip_ban events"
        # forever: a monitor green because it is blind. It shipped that way and was caught the
        # same day (2026-08-23) by running the selector against live Loki over a window
        # containing a KNOWN ban. Same lesson as the kube-state-metrics label trap in this role's
        # CLAUDE.md — a fail-open arm cannot tell "nothing to report" from "I asked the wrong
        # question".
        HA_BAN_SELECTOR='{namespace="homelab",container="home-assistant"} |~ "Banned IP"',
    )
