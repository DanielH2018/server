"""The app-facing half of monitor-bridge's configuration.

n8n, the *arr stack, the etcd restore drill, the
staging-gate backfill, Home Assistant, Airflow and the deploy-pipeline exporter — every threshold read by a check in
`checks/service.py`.

Field justifications sit beside the declarations, env var names beside the reads, and the values
in `templates/config/env.yaml.j2`.
Composed into `Config` by `bridge/config.py`; imports nothing from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceConfig:
    """n8n, the *arr stack, the drills, the deploy pipeline and Home Assistant."""

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
    ETCD_DRILL_STATE_DIR: str
    ETCD_DRILL_MAX_AGE_S: float
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
        # n8n hides successful executions, so the per-workflow failure streak accumulates across
        # cycles in src.state.n8n_streaks: it advances once per new error and resets when the latest
        # error ages past N8N_FAIL_WINDOW.
        N8N_FAIL_WINDOW=_env("N8N_FAIL_WINDOW"),
        N8N_CONSECUTIVE_MAX=_int("N8N_CONSECUTIVE_MAX"),
        # If N8N_SYSTEMIC_MAX or more workflows each fail N8N_SYSTEMIC_STREAK times, page once for
        # n8n itself instead of per workflow.
        N8N_SYSTEMIC_STREAK=_int("N8N_SYSTEMIC_STREAK"),
        N8N_SYSTEMIC_MAX=_int("N8N_SYSTEMIC_MAX"),
        # Polled for queue items sonarr flags "warning" (e.g. a poisoned .exe), which nothing else
        # pages. Empty key disables.
        SONARR_URL=_env("SONARR_URL").rstrip("/"),
        SONARR_API_KEY=_env("SONARR_API_KEY"),
        RADARR_URL=_env("RADARR_URL").rstrip("/"),
        RADARR_API_KEY=_env("RADARR_API_KEY"),
        # Consecutive cycles an *arr API must stay UNREACHABLE before paging, so a rollout (3 cycles
        # = 15 min) does not page but a dead *arr is named within the hour.
        # A queue item needing review still pages on sight.
        ARR_FETCH_CONSECUTIVE=_int("ARR_FETCH_CONSECUTIVE"),
        # 48h is upstream's own window: EpisodeTitleSpecification stops applying once `airDateUtc`
        # is over 48h old, so a later "TBA title" hold is stuck. See
        # verdicts/service.py:queue_warnings for what turns the grace off.
        ARR_TITLE_HOLD_GRACE_H=_num("ARR_TITLE_HOLD_GRACE_H"),
        # Bazarr holds its OWN copies of the *arr API keys on its PVC, so a rotation misses it and
        # its SignalR client loops into an OOM kill; the only signal is the OOM tile, which clears
        # an hour later.
        # Header spelling is `X-API-KEY`, not `X-Api-Key`.
        BAZARR_URL=_env("BAZARR_URL").rstrip("/"),
        BAZARR_API_KEY=_env("BAZARR_API_KEY"),
        # Prowlarr's health notification has no duration grace, so this goes down only when an
        # indexer has FAILED for PROWLARR_INDEXER_MIN_DOWN_MIN, aged from Prowlarr's own
        # initialFailure.
        # Empty key disables.
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
        ETCD_DRILL_STATE_DIR=_env("ETCD_DRILL_STATE_DIR", "/etcd-drill-state"),
        # DECIDED: 8 days, DERIVED from the drill's cadence rather than picked round. The cron
        # is k3s_etcd_restore_drill_cron = "20 10 * * 1" — weekly, Monday 10:20 — so anything
        # over 7 days means a scheduled run did not happen or did not pass. 8 gives one day of
        # slack for the run itself and for a check that evaluates just before the window, and no
        # more: a wider grace and the monitor clears the very miss it exists to catch, which is
        # how a 24h grace against a 23h gap reads green. Move this ONLY together
        # with the cron; the two are pinned to each other by
        # test_etcd_drill_grace_is_derived_from_the_cron.
        ETCD_DRILL_MAX_AGE_S=_num("ETCD_DRILL_MAX_AGE_DAYS", "8") * 86400,
        # Seconds, from the role defaults. The lag bound exceeds the master CI run many times over;
        # the fetch bound is 15 builder runs.
        BUILDER_MAX_LAG_S=_num("BUILDER_MAX_LAG_S"),
        BUILDER_MAX_FETCH_AGE_S=_num("BUILDER_MAX_FETCH_AGE_S"),
        # An HA automation stamps input_datetime.ha_heartbeat every minute, so a stale value catches
        # a wedged-but-running HA; 300s = 5 missed beats.
        # Plain float seconds because parse_duration belongs to the verdict layer. Empty URL/token
        # disables.
        HA_URL=_env("HA_URL").rstrip("/"),
        # File-mounted so the full-access long-lived token stays out of the env that envFrom cannot
        # filter; falls back to HA_TOKEN.
        HA_TOKEN=_env_file("HA_TOKEN", ""),
        HA_HEARTBEAT_MAX_AGE_S=_num("HA_HEARTBEAT_MAX_AGE"),
        HA_HEARTBEAT_ENTITY="input_datetime.ha_heartbeat",
        # 2 down cycles before `down`, so a planned HA redeploy (~120s unreachable) does not page.
        HA_CONSECUTIVE=_int("HA_CONSECUTIVE"),
        # ip_ban arm: HA bans by peer address, so five bad calls from the pod-network gateway
        # 10.42.0.1 ban it and crash-loop httpGet probes; the probes exec curl instead, which makes
        # a ban SILENT.
        # IT WATCHES THE BAN EVENT, NOT THE BAN STATE: `Banned IP` is logged once, so it pages for
        # HA_BAN_WINDOW and self-clears while the entry stays in /config/ip_bans.yaml.
        # A green check does not mean no IP is banned; the durable record is the Alertmanager
        # Discord notification. When it fires, check /config/ip_bans.yaml by hand.
        HA_BAN_WINDOW=_env("HA_BAN_WINDOW", "1h"),
        # `container=`, NOT `app=`: the k8s log stream has no `app` label, so `app=` matches nothing
        # and the arm reports no events forever.
        # Verify the selector against live Loki over a window containing a KNOWN ban.
        HA_BAN_SELECTOR='{namespace="homelab",container="home-assistant"} |~ "Banned IP"',
        # The token is read-only, so the env-secret carries it. Empty disables reads: the listener
        # still serves with no series, and DeployPipelineExporterAbsent pages.
        DEPLOY_PIPELINE_GITHUB_TOKEN=_env("DEPLOY_PIPELINE_GITHUB_TOKEN"),
        DEPLOY_PIPELINE_PORT=_int("DEPLOY_PIPELINE_PORT"),
        CHECK_EXPORTER_PORT=_int("CHECK_EXPORTER_PORT"),
        # Weekday 0 = Sunday, in UTC; the exporter derives homelab_deploy_reboot_window_active from
        # these.
        WEEKLY_REBOOT_MINUTE=_int("WEEKLY_REBOOT_MINUTE"),
        WEEKLY_REBOOT_HOUR=_int("WEEKLY_REBOOT_HOUR"),
        WEEKLY_REBOOT_WEEKDAY=_int("WEEKLY_REBOOT_WEEKDAY"),
        WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN=_int("WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN"),
        WEEKLY_REBOOT_MUTE_RECOVERY_MIN=_int("WEEKLY_REBOOT_MUTE_RECOVERY_MIN"),
    )
