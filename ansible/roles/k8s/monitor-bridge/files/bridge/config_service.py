"""The app-facing half of monitor-bridge's configuration.

n8n, the *arr stack, the deployer's own state files, the etcd restore drill, the
staging-gate backfill, Home Assistant and the deploy-pipeline exporter — every threshold read by a check in
`checks/service.py`.

Field justifications sit beside the declarations, env var names beside the reads, and the values
in `templates/config/env.yaml.j2`.
Composed into `Config` by `bridge/config.py`; imports nothing from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceConfig:
    """n8n, the *arr stack, the deployer state files, the drills and Home Assistant."""

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
    ARR_FETCH_CONSECUTIVE: int
    ARR_TITLE_HOLD_GRACE_H: float
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
    GITOPS_BEHIND_MAX_S: float
    GITOPS_CONTENTION_MAX_S: float
    BUILDER_MAX_LAG_S: float
    BUILDER_MAX_FETCH_AGE_S: float
    HA_URL: str
    HA_TOKEN: str = field(repr=False)
    HA_HEARTBEAT_MAX_AGE_S: float
    HA_HEARTBEAT_ENTITY: str
    HA_CONSECUTIVE: int
    HA_BAN_WINDOW: str
    HA_BAN_SELECTOR: str
    DEPLOY_PIPELINE_GITHUB_TOKEN: str = field(repr=False)
    DEPLOY_PIPELINE_PORT: int
    CHECK_EXPORTER_PORT: int
    WEEKLY_REBOOT_MINUTE: int
    WEEKLY_REBOOT_HOUR: int
    WEEKLY_REBOOT_WEEKDAY: int
    WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN: int
    WEEKLY_REBOOT_MUTE_RECOVERY_MIN: int


def service_config(
    _env: Callable[..., str],
    _int: Callable[..., int],
    _num: Callable[..., float],
    _env_file: Callable[..., str],
) -> ServiceConfig:
    """The app fields, read through the parsers `load_config` built over its environment."""
    return ServiceConfig(
        N8N_URL=_env("N8N_URL").rstrip("/"),
        N8N_API_KEY=_env("N8N_API_KEY"),
        # n8n hides successful executions (EXECUTIONS_DATA_SAVE_ON_SUCCESS=none, kept that way
        # to bound database.sqlite + its B2 backup churn), so "consecutive" can't be read from
        # one snapshot — the per-workflow failure streak is accumulated across cycles in
        # src.state.n8n_streaks (see n8n_update_streaks): it advances once per NEW error (deduped by
        # execution id) and resets when a workflow's latest error ages past N8N_FAIL_WINDOW
        # (recovered / went idle).
        N8N_FAIL_WINDOW=_env("N8N_FAIL_WINDOW"),
        N8N_CONSECUTIVE_MAX=_int("N8N_CONSECUTIVE_MAX"),
        # Systemic catch: if N8N_SYSTEMIC_MAX+ workflows are each failing >=
        # N8N_SYSTEMIC_STREAK times, something is wrong with n8n itself — page now as ONE alert
        # instead of waiting for each to reach N8N_CONSECUTIVE_MAX (and instead of a
        # per-workflow flood).
        N8N_SYSTEMIC_STREAK=_int("N8N_SYSTEMIC_STREAK"),
        N8N_SYSTEMIC_MAX=_int("N8N_SYSTEMIC_MAX"),
        # Sonarr/Radarr queue warnings: the 2026-07-01 incident — an indexer served a poisoned
        # fake-episode .exe, sonarr itself blocked the import and flagged the queue item
        # trackedDownloadStatus "warning" (message: "Caution: Found executable file with
        # extension: '.exe'") — but nothing paged, so the release sat seeding for a full day
        # before a manual review caught it. Polled directly (X-Api-Key header), same "internal
        # REST API, empty key disables" idiom as N8N_API_KEY.
        SONARR_URL=_env("SONARR_URL").rstrip("/"),
        SONARR_API_KEY=_env("SONARR_API_KEY"),
        RADARR_URL=_env("RADARR_URL").rstrip("/"),
        RADARR_API_KEY=_env("RADARR_API_KEY"),
        # Consecutive cycles an *arr API must stay UNREACHABLE before check_arr_queue pages —
        # the fetch alone; a queue item needing review still pages on the cycle it is seen.
        # A rolling radarr refuses connections for as long as its pod takes to come back, and
        # three of this monitor's DOWN episodes in the 30 days to 2026-09-11 were exactly that,
        # each co-timed with a k3s Workload Health `radarr(1)` episode. 3 cycles = 15 min at
        # INTERVAL=300, which outlasts a rollout and still names a genuinely dead *arr inside
        # the hour. This REPLACED arr_queue's STARTUP_GRACE membership rather than stacking on
        # it: that grace covered the same transient at 2 cycles and covered the queue verdict
        # too, which this deliberately does not.
        ARR_FETCH_CONSECUTIVE=_int("ARR_FETCH_CONSECUTIVE"),
        # Hours a queue item on Sonarr's own self-clearing title hold is held before it pages
        # (#2786). 48 is upstream's OWN window, not a taste: EpisodeTitleSpecification stops
        # applying the rule once the episode's `airDateUtc` is more than 48h old, so an item
        # still carrying "Episode has a TBA title and recently aired" past that is stuck rather
        # than waiting. Measured 2026-09-27: three of arr_queue's seven DOWN episodes over the
        # preceding 14 days were this hold (2.9h, 7.4h, 2.9h) and named no action an operator
        # could take; the other four were Custom Format rejections and still page on sight. The
        # grace is narrow by construction — see verdicts/service.py's queue_warnings for the
        # four conditions that turn it off.
        ARR_TITLE_HOLD_GRACE_H=_num("ARR_TITLE_HOLD_GRACE_H"),
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
        BAZARR_URL=_env("BAZARR_URL").rstrip("/"),
        BAZARR_API_KEY=_env("BAZARR_API_KEY"),
        # Prowlarr sustained-indexer watchdog: Prowlarr's in-app health notification is binary —
        # with warnings on every indexer flap pages, with warnings off only the
        # all-indexers-down red error fires; there's no duration grace. We poll
        # /api/v1/indexerstatus and go `down` only when an indexer has been FAILING for >=
        # PROWLARR_INDEXER_MIN_DOWN_MIN (age from Prowlarr's own initialFailure, so it survives
        # a monitor-bridge redeploy), suppressing the sub-threshold flaps public trackers throw
        # that self-clear inside Prowlarr's ~5-15min backoff. Empty key = disabled (stays up),
        # same idiom as N8N_API_KEY. Already on `media`, so prowlarr:9696 is reachable.
        PROWLARR_URL=_env("PROWLARR_URL").rstrip("/"),
        PROWLARR_API_KEY=_env("PROWLARR_API_KEY"),
        PROWLARR_INDEXER_MIN_DOWN_MIN=_num("PROWLARR_INDEXER_MIN_DOWN_MIN"),
        # Comma-separated indexer names (case-insensitive) never counted as offenders. For
        # chronically flaky PUBLIC trackers whose backend routinely 503s/times-out past the
        # sustained-down gate (e.g. The Pirate Bay's apibay.org) — they'd page every outage
        # though the other indexers cover the same searches. Prowlarr's own all-indexers-down
        # onHealthIssue is the backstop if every indexer, ignored or not, fails at once.
        # Empty = ignore nothing.
        PROWLARR_INDEXER_IGNORE=_env("PROWLARR_INDEXER_IGNORE"),
        # Since the Docker uninstall (2026-08-14) this reads daniel-box's own deployer state —
        # the pod is pinned to that node and hostPath-mounts /var/lib/gitops-deploy. One
        # deployer remains in the fleet (the Pi runs has_gitops: false), so one watcher.
        GITOPS_STATE_DIR=_env("GITOPS_STATE_DIR"),
        GITOPS_MAX_AGE_S=_num("GITOPS_MAX_AGE_MIN") * 60,
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
        # How long the host may sit behind origin before GitOps Status pages. Generous on
        # purpose: the deployer ticks every 30 min, and the dirty-tree path (operator mid-edit)
        # is behind by design for as long as the edit lasts. 6 h pages a genuinely-stuck host
        # well inside a day while never firing on a normal push or a long editing session.
        GITOPS_BEHIND_MAX_S=_num("GITOPS_BEHIND_MAX_MIN") * 60,
        # How long consecutive ticks may defer on one busy service lock before GitOps Status
        # pages (issue #1847). Derived from the longest deploy the deployer itself is allowed
        # — `gitops_deploy_broad_timeout_s`, 1800 s, the same ceiling `SERVICE_LOCK_WAIT_S`
        # gives one wait: a holder still on the lock 30 min after the first tick gave up has
        # outlived every legitimate deploy. The behind arm above would page the same fault,
        # but only after six hours sized for a dirty tree.
        # The rendered value is `gitops_markers.CONTENTION_PAGE_SECONDS` in minutes, the figure
        # the SessionStart banner names a streak at, so the banner and this page cannot
        # disagree about a lock; ansible/tests/deploy/test_gitops_markers_copies.py pins it.
        GITOPS_CONTENTION_MAX_S=_num("GITOPS_CONTENTION_MAX_MIN") * 60,
        # The host-package builder's two page bounds, in seconds, rendered from the role defaults
        # `monitor_bridge_builder_max_lag_s` and `monitor_bridge_builder_max_fetch_age_s`. The
        # lag bound is how long master may sit unbuilt: it exceeds the ~103 s master CI run many
        # times over, so a normal landing never trips it. The template raises the default to
        # the rollout canary's longest gap between updater fires (`updater_lag_grace_s`, #6530). The fetch bound is 15 builder runs
        # (the timer fires every 2 minutes), so one slow `git fetch` never trips it either.
        BUILDER_MAX_LAG_S=_num("BUILDER_MAX_LAG_S"),
        BUILDER_MAX_FETCH_AGE_S=_num("BUILDER_MAX_FETCH_AGE_S"),
        # HA automation-engine heartbeat: an HA time_pattern automation stamps
        # input_datetime.ha_heartbeat with now() every minute, so its last_changed is fresh ONLY
        # while HA's automation scheduler is executing. We poll HA's /api/states over the apps
        # network (Bearer token) and go down when it's stale — catching a wedged-but-running HA
        # (HTTP :8123 up, scheduler stuck) that the container healthcheck can't see. Empty
        # URL/token = disabled (stays up), like N8N_API_KEY/PI_ORIGIN. 300s = 5 missed
        # 1-min beats; rides out an HA restart/deploy. Seconds (no unit suffix) — kept a plain
        # float here because parse_duration belongs to the verdict layer, not to config.
        HA_URL=_env("HA_URL").rstrip("/"),
        # File-mounted (HA_TOKEN_FILE) so this full-access HA long-lived token stays out of the
        # pod environment envFrom cannot filter; falls back to the HA_TOKEN env.
        HA_TOKEN=_env_file("HA_TOKEN", ""),
        HA_HEARTBEAT_MAX_AGE_S=_num("HA_HEARTBEAT_MAX_AGE"),
        HA_HEARTBEAT_ENTITY="input_datetime.ha_heartbeat",
        # Consecutive-cycle hysteresis (like CPU_CONSECUTIVE) so a planned HA redeploy — which
        # takes the API unreachable for ~120s and then leaves the scheduler a beat behind —
        # doesn't page. 2 straight down cycles (~one full INTERVAL of continuous badness) before
        # `down`.
        HA_CONSECUTIVE=_int("HA_CONSECUTIVE"),
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
        # The deploy-pipeline exporter (`deploy_pipeline.py`). The token is read-only, Actions
        # read and Variables read on this repository, so the env-secret carries it rather than a
        # file mount. Empty disables the family's reads: the listener still serves, with no
        # series, and DeployPipelineExporterAbsent pages that.
        DEPLOY_PIPELINE_GITHUB_TOKEN=_env("DEPLOY_PIPELINE_GITHUB_TOKEN"),
        DEPLOY_PIPELINE_PORT=_int("DEPLOY_PIPELINE_PORT"),
        CHECK_EXPORTER_PORT=_int("CHECK_EXPORTER_PORT"),
        # The weekly reboot's cron fields (weekday 0 = Sunday, in UTC), the minutes from the
        # cron's fire to the reboot, and the minutes M2's mute stays open after it. The
        # exporter's homelab_deploy_reboot_window_active derives from them.
        WEEKLY_REBOOT_MINUTE=_int("WEEKLY_REBOOT_MINUTE"),
        WEEKLY_REBOOT_HOUR=_int("WEEKLY_REBOOT_HOUR"),
        WEEKLY_REBOOT_WEEKDAY=_int("WEEKLY_REBOOT_WEEKDAY"),
        WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN=_int("WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN"),
        WEEKLY_REBOOT_MUTE_RECOVERY_MIN=_int("WEEKLY_REBOOT_MUTE_RECOVERY_MIN"),
    )
