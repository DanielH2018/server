"""Log-pipeline checks for monitor-bridge — Loki ingestion, shipper drops, Loki reachability.

Slice 3 of the check.py split. Reads config as `cfg.X` and the fetch layer as `bridge.net.X`,
so the tests' patches on those modules reach it; the verdicts it from-imports are patched on
THIS module (`monkeypatch.setattr(checks.logs, "log_error_verdict", ...)`), because this is
where they are bound. Rule and enforcement: bridge/config.py's header.

`with_log_errors` lives here rather than beside `check_k8s_workloads`, its only caller,
because it is a Loki arm folded into a cluster verdict — the caller reaches it as
`checks.logs.with_log_errors` and test_check_loki.py patches it here.
"""

from bridge.config import Config
import bridge.net
from verdicts.cluster import log_error_verdict
from verdicts.logs import (
    kuma_notify_failures,
    loki_ingestion_fresh,
    shipper_dropped,
    swallowed_verdicts,
)


def check_loki_ingestion(cfg: Config) -> tuple[bool, str]:
    """Checks that all three Loki ingestion arms (file-tail, container stream, Pi) are fresh.

    Down if any arm is silent: the file-tail union, the docker-stream arm (a
    docker_sd-specific break the file-tail selector excludes), and the Pi's own stream
    (uncounted by the other two, so a dead Pi promtail would otherwise be invisible).
    Returns (ok, msg).
    """
    # Two arms, down if EITHER pipeline is silent: the file-tail union (arm 1) catches a
    # file-tail break (all of authlog/syslog/traefik going silent — a total promtail death or
    # a static_configs/bind regression) over a tolerant window; the container-stream arm
    # (arm 2) catches a docker_sd-specific break the file-tail selector excludes (see
    # LOKI_DOCKER_STREAM). The docker stream dwarfs the file-tail streams, so arm 1 must NOT
    # include it (else a healthy docker stream masks a dead file-tail pipeline) — hence the
    # separate selector + wider window (LOKI_FILETAIL_WINDOW).
    ok_all, msg_all = loki_ingestion_fresh(
        bridge.net.loki_count(cfg, cfg.LOKI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_all:
        return False, "file-tail streams silent — " + msg_all
    ok_docker, msg_docker = loki_ingestion_fresh(
        bridge.net.loki_count(cfg, cfg.LOKI_DOCKER_STREAM, cfg.LOKI_WINDOW),
        cfg.LOKI_WINDOW,
    )
    if not ok_docker:
        return False, "container log stream silent — " + msg_docker
    # Arm 3: the Pi ships its own logs and neither arm above counts them, so its promtail
    # dying is invisible while the cluster keeps talking.
    ok_pi, msg_pi = loki_ingestion_fresh(
        bridge.net.loki_count(cfg, cfg.LOKI_PI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_pi:
        return False, "daniel-pi log stream silent — " + msg_pi
    return True, "%s (+ container stream, + pi)" % msg_all


def check_shipper_dropped(cfg: Config) -> tuple[bool, str]:
    """Prometheus-based log-shipper + Loki-distributor partial-loss watchdog. Prom-dependent.

    Reads BOTH sides of the pipe (see shipper_dropped): the shippers' own client-side
    dropped-entries counter, and Loki's server-side `loki_discarded_samples_total`, broken
    down `by (reason)` so a fired alert can name the cause. The client-side counter alone
    missed a 161,573-sample burst on 2026-09-03 — Loki discarded it without any shipper ever
    attributing the loss to itself. Both queries use a `__name__` regex rather than a bare
    metric name, the same reason SHIPPER_DROPPED_METRICS does: a counter rename on either side
    must not silently read as "0 dropped forever".
    """
    client_count = bridge.net.prom_scalar(
        cfg,
        'sum(increase({__name__=~"%s"}[%s]))'
        % (cfg.SHIPPER_DROPPED_METRICS, cfg.SHIPPER_DROPPED_WINDOW),
    )
    server_reasons = [
        (labels.get("reason", "unknown"), value)
        for labels, value in bridge.net.prom_vector(
            cfg,
            'sum by (reason) (increase({__name__=~"%s"}[%s]))'
            % (cfg.SHIPPER_DROPPED_SERVER_METRIC, cfg.SHIPPER_DROPPED_WINDOW),
        )
    ]
    return shipper_dropped(
        client_count,
        server_reasons,
        cfg.SHIPPER_DROPPED_WINDOW,
        cfg.SHIPPER_DROPPED_MAX,
    )


def check_loki_reachable(cfg: Config) -> tuple[bool, str]:
    bridge.net.loki_reachable(cfg)
    return True, "Loki reachable"


def with_log_errors(cfg: Config, ok: bool, msg: str) -> tuple[bool, str]:
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
    if not cfg.LOG_ERROR_SELECTOR:
        return ok, msg
    ignore = {n.strip().lower() for n in cfg.LOG_ERROR_IGNORE.split(",") if n.strip()}
    try:
        matches, total = bridge.net.log_error_counts(
            cfg, cfg.LOG_ERROR_SELECTOR, cfg.LOG_ERROR_PATTERN, cfg.LOG_ERROR_WINDOW
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
# and the one the Pi's promtail gives its health.log (alerts.py's `SYSLOG_ALERT_LOGQL` reads the
# same stream).
SWALLOWED_VERDICTS_LOGQL = '{job="syslog"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
# The one pusher that is a pod, not a host cron: pi-peer-backup's CronJob container (#1943).
# It has no `logger`, so it echoes its lines in the syslog shape above and Alloy lands them
# under the pod labels (`job="k8s"`, `container="pull"`), where the selector above cannot see
# them. A second, narrow selector rather than `job=~"syslog|k8s"`: the line cap below was
# sized against the syslog stream alone, and every pod log in the cluster would count toward
# it. The container name is the CronJob's (`pi-peer-backup/templates/cronjob.yaml.j2`).
SWALLOWED_VERDICTS_POD_LOGQL = '{container="pull"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
# ~9x the population measured 2026-09-17 (529 lines / 3h) — see bridge.net.loki_lines.
SWALLOWED_VERDICTS_LIMIT = 5000


def check_swallowed_verdicts(cfg: Config) -> tuple[bool, str]:
    """A host cron's DOWN verdict that kuma-push-lib.sh logged and then lost (#1869).

    The library returns 0 after a failed push by design — a non-zero exit would fail the cron
    for an event already logged — so a swallowed verdict reaches nobody until the tile's
    heartbeat deadline, a day and an hour later for the daily drift producers. This reads the
    library's own final-failure line out of Loki and pages within one cycle.

    FAILS OPEN on a fetch error, on top of being in LOKI_DEPENDENT. The gate probes
    `/loki/api/v1/labels`, which answers fast while a range query is the thing a busy Loki
    is slow at, so a raise here would page this tile for a slow Loki rather than a lost
    verdict. The tile's heartbeat deadline is still the backstop for the cycle this skips.
    """
    window_s = cfg.SWALLOWED_VERDICTS_WINDOW_S
    try:
        lines = bridge.net.loki_lines(
            cfg, SWALLOWED_VERDICTS_LOGQL, window_s, SWALLOWED_VERDICTS_LIMIT
        )
    except Exception as e:
        return (
            True,
            "swallowed-verdict scan unavailable (%s) — Loki Reachable owns a Loki fault"
            % e,
        )
    # Its own try: a failure here costs the one pod pusher's coverage for a cycle, not the
    # syslog arm's, and the message says so rather than reading clean.
    pod_note = ""
    try:
        pod_lines = bridge.net.loki_lines(
            cfg, SWALLOWED_VERDICTS_POD_LOGQL, window_s, SWALLOWED_VERDICTS_LIMIT
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


# Kuma's own failed-send lines, from its container log (#1891, #1895). `{container="uptime-kuma"}`
# is the label Alloy gives the pod's stdout/stderr on the cluster. Two lines per drop, both at
# ERROR level: `Cannot send notification to <name>` and then the error itself, `ERROR: Error:
# Request failed with status code 429 …` — so the second alternative is anchored on the ERROR
# tag (with room for the colour reset between tag and message), which keeps Kuma's WARN-level
# `Pending: Request failed with status code 500` monitor probes out of the fetch. Measured
# 2026-09-17: 148 lines over 14 days, 74 drops and 74 reasons.
KUMA_NOTIFY_FAILURES_LOGQL = (
    '{container="uptime-kuma"} |~ "Cannot send notification|ERROR:.{0,8} Error: "'
)
# A long multi-tile outage resends every tile on its own beat count, so a burst of drops
# clusters around a resend; 500 is far above any burst 76 tiles can produce in one window,
# counting the reason line each drop brings with it.
KUMA_NOTIFY_FAILURES_LIMIT = 500


def check_kuma_notify_failures(cfg: Config) -> tuple[bool, str]:
    """A notification Kuma tried to send and dropped — a Discord 429, a dead SMTP login (#1891).

    Kuma logs `Cannot send notification to <name>` and does not retry, so the transition or
    resend that line stands for reached nobody. check_discord GET-verifies that the webhook
    exists and cannot see a dropped POST. This reads Kuma's own lines out of Loki — the drop
    and the reason it logs right after it — and pages on the tile that notifies email as well
    as Discord, so a dropped Discord send is not reported over the channel that dropped it.

    FAILS OPEN on a fetch error, on top of being in LOKI_DEPENDENT, for the reason
    check_swallowed_verdicts gives: the gate probes `/labels`, and a range query is what a
    busy Loki is slow at.
    """
    window_s = cfg.KUMA_NOTIFY_FAILURES_WINDOW_S
    try:
        lines = bridge.net.loki_lines(
            cfg, KUMA_NOTIFY_FAILURES_LOGQL, window_s, KUMA_NOTIFY_FAILURES_LIMIT
        )
    except Exception as e:
        return (
            True,
            "dropped-notification scan unavailable (%s) — Loki Reachable owns a Loki fault"
            % e,
        )
    return kuma_notify_failures(
        lines,
        "%dh" % (window_s // 3600) if window_s % 3600 == 0 else "%ds" % window_s,
        truncated=len(lines) >= KUMA_NOTIFY_FAILURES_LIMIT,
    )
