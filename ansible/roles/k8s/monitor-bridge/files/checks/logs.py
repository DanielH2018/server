"""Log-pipeline checks for monitor-bridge — swallowed verdicts and the workload log arm.

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
from verdicts.cluster import log_error_verdict
from verdicts.logs import swallowed_verdicts


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
# The pushers that are pods, not host cron: pi-peer-backup's CronJob container (#1943),
# crowdsec's crowdsec-jobs sidecar, which runs the remote allowlist and the AppSec verify since
# H2.4b, and the configarr-health and janitorr-health CronJobs, host crons until H2.5b and
# H2.5c (#5569). None has `logger`, so each echoes its lines in the syslog shape above and Alloy
# lands them under the pod labels (`job="k8s"`, `container="<name>"`), where the selector
# above cannot see them. A second, narrow selector rather than `job=~"syslog|k8s"`: the line cap
# below was sized against the syslog stream alone, and every pod log in the cluster would
# count toward it. The container name is the one its template declares (`pi-peer-backup/
# templates/cronjob.yaml.j2`, `crowdsec/templates/deployment.yaml.j2` and `health-cronjob.yaml.j2`
# in configarr and janitorr), and each is the workload's own name rather than a generic one:
# `container` is the only label here that ties the stream to that pod, so any other workload
# naming a container the same would be read as this pusher (#1976).
SWALLOWED_VERDICTS_POD_LOGQL = '{container=~"pi-peer-backup|crowdsec-jobs|configarr-health|janitorr-health"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
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
