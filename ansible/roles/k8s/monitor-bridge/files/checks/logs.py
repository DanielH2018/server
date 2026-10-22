"""Log-pipeline checks for monitor-bridge — shipper drops, swallowed verdicts, the workload log arm.

Slice 3 of the check.py split. Reads config as `cfg.X` and every query through the `src`
argument (`bridge.sources.Sources`), so a test hands in a fake; the verdicts it from-imports
are patched on THIS module (`monkeypatch.setattr(checks.logs, "log_error_verdict", ...)`), because this is
where they are bound. Rule and enforcement: bridge/config.py's header.

`check_workload_log_errors` is the one caller of `with_log_errors`. The Kubernetes arms it was
once folded into are the k8s-workloads alert rules, so it is a Loki arm on a row of its own.
test_check_loki.py patches `with_log_errors` here, where it is bound.
"""

from collections.abc import Callable

from bridge.common import host_uptime_s
from bridge.config import Config
from bridge.sources import Sources
from bridge.parsing import duration_seconds
from verdicts.cluster import log_error_verdict
from verdicts.logs import (
    otelcol_export_failures,
    shipper_dropped,
    swallowed_verdicts,
    tempo_poll_errors,
)


def check_shipper_dropped(
    cfg: Config,
    src: Sources,
    uptime_s: Callable[[], float | None] = host_uptime_s,
) -> tuple[bool, str]:
    """Prometheus-based log-shipper + Loki-distributor partial-loss watchdog. Prom-dependent.

    Reads BOTH sides of the pipe (see shipper_dropped): the shippers' own client-side
    dropped-entries counter, and Loki's server-side `loki_discarded_samples_total`, broken
    down `by (reason)` so a fired alert can name the cause. The client-side counter alone
    missed a 161,573-sample burst on 2026-09-03 — Loki discarded it without any shipper ever
    attributing the loss to itself. Both queries use a `__name__` regex rather than a bare
    metric name, the same reason SHIPPER_DROPPED_METRICS does: a counter rename on either side
    must not silently read as "0 dropped forever".
    """
    # `uptime_s` and `src` are the seams, so a test states a boot time and a pair of metric
    # answers instead of patching this module — the rule `with_pi_ports` follows with
    # `tcp_open`, and what keeps the grace below from being provable only by inspection.
    # The node's uptime, not this pod's age: an ordinary deploy restarts the bridge without
    # rebooting anything, and both reboot arms below cover a fault only a reboot produces. An
    # unreadable /proc/uptime reads as no grace, so the check evaluates normally.
    uptime = uptime_s()
    # Never read back past the reboot (#3490), the rule check_swallowed_verdicts follows. While
    # Loki is down for the weekly restart, daniel-pi's Alloy keeps shipping and drops what Loki
    # refuses: 179,396 `ingester_error` entries on 2026-10-04. The Pi pushes through Traefik on
    # daniel-box, this pod's node, so its loss cannot outlast that node's boot. The tile
    # recovered at 08:39:30 against the 1h lookback, so the drops ended by ~07:39, before
    # daniel-box finished booting at 07:46:35, yet the lookback held them in range 24 minutes
    # past the Kuma maintenance window. Inside BOOT_SETTLE_S the shipper arms are skipped;
    # after it the lookback grows back from the end of the settle window to its configured
    # length, so a drop after the reboot still pages. The export-failure arm keeps its own window and stays
    # live throughout.
    window = cfg.SHIPPER_DROPPED_WINDOW
    if uptime is not None:
        since_settle = int(uptime - cfg.BOOT_SETTLE_S)
        if since_settle <= 0:
            return with_telemetry_arms(
                cfg,
                src,
                True,
                "shipper drops skipped — the node booted %ds ago, inside BOOT_SETTLE_S "
                "(%ds): the reboot's own drops are owned by the reboot, not by this tile"
                % (int(uptime), cfg.BOOT_SETTLE_S),
            )
        try:
            configured_s = duration_seconds(window)
        except ValueError:
            # A window this cannot read keeps its configured text: Prometheus parses the
            # query, and a malformed range then fails there, where it already did.
            configured_s = None
        if configured_s is not None and since_settle < configured_s:
            window = "%ds" % since_settle
    client_count = src.prom_scalar(
        'sum(increase({__name__=~"%s"}[%s]))' % (cfg.SHIPPER_DROPPED_METRICS, window),
    )
    server_reasons = [
        (labels.get("reason", "unknown"), value)
        for labels, value in src.prom_vector(
            'sum by (reason) (increase({__name__=~"%s"}[%s]))'
            % (cfg.SHIPPER_DROPPED_SERVER_METRIC, window),
        )
    ]
    ok, msg = shipper_dropped(
        client_count,
        server_reasons,
        window,
        cfg.SHIPPER_DROPPED_MAX,
        backlog_grace_active=uptime is not None
        and uptime < cfg.SHIPPER_BACKLOG_GRACE_S,
    )
    return with_telemetry_arms(cfg, src, ok, msg)


def with_telemetry_arms(
    cfg: Config, src: Sources, ok: bool, msg: str
) -> tuple[bool, str]:
    """Fold the collector and Tempo arms into the shipper verdict, the order the data flows.

    Both return paths of check_shipper_dropped go through here, so neither arm is skipped
    inside BOOT_SETTLE_S: a Tempo restart after a reboot is when a half-written block bites.
    """
    ok, msg = with_export_failures(cfg, src, ok, msg)
    return with_tempo_poll_errors(cfg, src, ok, msg)


def with_export_failures(
    cfg: Config, src: Sources, ok: bool, msg: str
) -> tuple[bool, str]:
    """Fold the collector's export-failure arm into the shipper verdict, a failure winning.

    Folded here rather than given its own monitor, the reason `with_log_errors` and the
    extended-resource arm were: a new Kuma monitor needs a new push token in SOPS, and this arm
    asks the question the two arms above it already ask — did telemetry get lost between a
    producer and its backend. The two above it read the LOG pipe, client side and server side;
    this one reads the third producer on the same estate, the OTel collector, which exports
    Claude Code's logs, metrics and traces to Loki, Prometheus and Tempo.

    Arrived 2026-10-01 from `observability/templates/telemetry-health.sh.j2`, which ran the same
    query from a host cron with its own Prometheus client (#3094). Its own window and threshold,
    because an export failure is not log-line churn at any rate — see
    `OTELCOL_SEND_FAILED_WINDOW`/`_MAX` in `bridge/config_io.py`.

    An empty `OTELCOL_SEND_FAILED_METRICS` disables the arm, the convention every check here
    follows for an unset selector. Prom-dependent like its caller, so no gate membership
    changes: `shipper_dropped` is already in `PROM_DEPENDENT` and an unreachable Prometheus
    suppresses the whole check.
    """
    if not cfg.OTELCOL_SEND_FAILED_METRICS:
        return ok, msg
    failed = src.prom_scalar(
        'sum(increase({__name__=~"%s"}[%s]))'
        % (cfg.OTELCOL_SEND_FAILED_METRICS, cfg.OTELCOL_SEND_FAILED_WINDOW),
    )
    export_ok, export_msg = otelcol_export_failures(
        failed,
        cfg.OTELCOL_SEND_FAILED_WINDOW,
        cfg.OTELCOL_SEND_FAILED_MAX,
    )
    if export_ok:
        return ok, "%s, %s" % (msg, export_msg)
    return False, "%s | %s" % (export_msg, msg)


def with_tempo_poll_errors(
    cfg: Config, src: Sources, ok: bool, msg: str
) -> tuple[bool, str]:
    """Fold Tempo's blocklist-poll arm into the shipper verdict, a failure winning.

    The arms above ask whether telemetry was lost on its way to a backend. This one asks
    whether Tempo can serve what arrived: a failed poll leaves the blocklist empty after a
    restart, so every stored trace is unsearchable and retention deletes nothing, while the
    pod stays Ready and ingest looks healthy (#4626 ran a week unnoticed). Folded here for the
    reason with_export_failures was: a new monitor needs a new push token in SOPS.

    Prom-dependent like its caller. An empty TEMPO_POLL_ERRORS_METRIC disables the arm.
    """
    if not cfg.TEMPO_POLL_ERRORS_METRIC:
        return ok, msg
    errors = src.prom_scalar(
        "sum(increase(%s[%s]))"
        % (cfg.TEMPO_POLL_ERRORS_METRIC, cfg.TEMPO_POLL_ERRORS_WINDOW),
    )
    tempo_ok, tempo_msg = tempo_poll_errors(
        errors, cfg.TEMPO_POLL_ERRORS_WINDOW, cfg.TEMPO_POLL_ERRORS_MAX
    )
    if tempo_ok:
        return ok, "%s, %s" % (msg, tempo_msg)
    return False, "%s | %s" % (tempo_msg, msg)


def check_workload_log_errors(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Pages when a container logs a burst of fatal lines, however healthy its pod reads.

    The Kubernetes readiness arms moved to the k8s-workloads alert rules. This arm stays because
    it reads Loki, and the log rules belong on the loki-homelab ruler (M6's batch A7 ports it).
    The row's gate is `startup_grace`, so the arm pages on its second consecutive bad cycle.
    """
    return with_log_errors(cfg, src, True, "workload logs")


def with_log_errors(cfg: Config, src: Sources, ok: bool, msg: str) -> tuple[bool, str]:
    """Fold the log-pattern arm into a verdict, a burst winning the message.

    A container that is Ready while failing at its job reads healthy to every arm that measures
    Kubernetes state, because by their measure it is. A burst of fatal log lines is the signal
    those arms cannot see.

    FAILS OPEN on a Loki error, and the rows that call it are gated by `startup_grace` alone.
    Same reasoning as ha_heartbeat's ban arm.
    """
    if not cfg.LOG_ERROR_SELECTOR:
        return ok, msg
    ignore = {n.strip().lower() for n in cfg.LOG_ERROR_IGNORE.split(",") if n.strip()}
    try:
        matches, total = src.log_error_counts(
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


# Every push-outcome line the host crons emit, and nothing else: the cron's own
# `status=<up|down>` verdict line and kuma-push-lib.sh's final `push failed (` line. The
# transient-retry line is excluded here rather than in Python so it never counts toward the
# fetch cap. `{job="syslog"}` is the label Alloy puts on `logger` lines on both cluster hosts
# and the one the Pi's Alloy gives its health.log (alerts.py's `SYSLOG_ALERT_LOGQL` reads the
# same stream).
SWALLOWED_VERDICTS_LOGQL = '{job="syslog"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
# The pushers that are pods, not host cron: pi-peer-backup's CronJob container (#1943) and
# crowdsec's crowdsec-jobs sidecar, which runs the remote allowlist and the AppSec verify since
# H2.4b. Neither has `logger`, so each echoes its lines in the syslog shape above and Alloy
# lands them under the pod labels (`job="k8s"`, `container="<name>"`), where the selector
# above cannot see them. A second, narrow selector rather than `job=~"syslog|k8s"`: the line cap
# below was sized against the syslog stream alone, and every pod log in the cluster would
# count toward it. The container name is the CronJob's (`pi-peer-backup/templates/
# cronjob.yaml.j2` and `crowdsec/templates/deployment.yaml.j2`), and each is the workload's own
# name rather than a generic one: `container` is the only label here that ties the stream to
# that pod, so any other workload naming a container the same would be read as this pusher
# (#1976).
SWALLOWED_VERDICTS_POD_LOGQL = '{container=~"pi-peer-backup|crowdsec-jobs"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
# ~9x the population measured 2026-09-17 (529 lines / 3h) — see Sources.loki_lines.
SWALLOWED_VERDICTS_LIMIT = 5000


def check_swallowed_verdicts(
    cfg: Config,
    src: Sources,
    uptime_s: Callable[[], float | None] = host_uptime_s,
) -> tuple[bool, str]:
    """A host cron's DOWN verdict that kuma-push-lib.sh logged and then lost (#1869).

    The library returns 0 after a failed push by design — a non-zero exit would fail the cron
    for an event already logged — so a swallowed verdict reaches nobody until the tile's
    heartbeat deadline, a day and an hour later for the daily drift producers. This reads the
    library's own final-failure line out of Loki and pages within one cycle.

    FAILS OPEN on a fetch error, and its row's gate is `startup_grace`. A raise here would
    page this tile for a slow Loki rather than a lost verdict, and the loki-homelab ruler's
    `LokiHomelabMetricsMissing` pages a Loki fault. The tile's heartbeat deadline is still the
    backstop for the cycle this skips.
    """
    # Never read back past the reboot (#2783). uptime-kuma has no endpoint for ~16 minutes of the
    # weekly restart, so every host cron whose slot falls in there loses a push for one known
    # cause; a daily producer's lost verdict then stays the newest line for its tag until
    # tomorrow, and the 3h window held this tile red for 2.9h after everything else recovered.
    # Inside BOOT_SETTLE_S there is no window left to read, so the cycle is skipped; after it the
    # window grows back to its configured length, and the tile pages again on the first verdict
    # lost for any other reason.
    uptime = uptime_s()
    window_s = cfg.SWALLOWED_VERDICTS_WINDOW_S
    if uptime is not None and uptime - cfg.BOOT_SETTLE_S < window_s:
        since_settle = int(uptime - cfg.BOOT_SETTLE_S)
        if since_settle <= 0:
            return True, (
                "skipped — the node booted %ds ago, inside BOOT_SETTLE_S (%ds): the reboot's own "
                "lost pushes are owned by the reboot, not by this tile"
                % (int(uptime), cfg.BOOT_SETTLE_S)
            )
        window_s = since_settle
    try:
        lines = src.loki_lines(
            SWALLOWED_VERDICTS_LOGQL, window_s, SWALLOWED_VERDICTS_LIMIT
        )
    except Exception as e:
        return (
            True,
            "swallowed-verdict scan unavailable (%s) — a Loki fault is the ruler's page"
            % e,
        )
    # Its own try: a failure here costs the one pod pusher's coverage for a cycle, not the
    # syslog arm's, and the message says so rather than reading clean.
    pod_note = ""
    try:
        pod_lines = src.loki_lines(
            SWALLOWED_VERDICTS_POD_LOGQL, window_s, SWALLOWED_VERDICTS_LIMIT
        )
    except Exception as e:
        pod_lines = []
        pod_note = " (pod-stream fetch unavailable: %s)" % e
    # The verdict keeps the newest line per tag by timestamp, so the merge needs no ordering.
    ok, msg = swallowed_verdicts(
        lines + pod_lines,
        "%dh" % (window_s // 3600) if window_s % 3600 == 0 else "%ds" % window_s,
        truncated=max(len(lines), len(pod_lines)) >= SWALLOWED_VERDICTS_LIMIT,
    )
    return ok, msg + pod_note
