"""Log-pipeline checks for monitor-bridge — swallowed verdicts.

Slice 3 of the check.py split. Reads config as `cfg.X` and every query through the `src`
argument (`bridge.sources.Sources`), so a test hands in a fake; the verdicts it from-imports
are patched on THIS module, because this is where they are bound. Rule and enforcement:
bridge/config.py's header.

The workload log-pattern arm that lived here is the `WorkloadLogErrors` rule on loki-homelab
(M6's batch A7).
"""

from collections.abc import Callable

from bridge.common import host_uptime_s
from bridge.config import Config
from bridge.sources import Sources
from verdicts.logs import swallowed_verdicts


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
