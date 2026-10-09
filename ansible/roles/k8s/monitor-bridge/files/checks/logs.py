"""Log-pipeline checks for monitor-bridge — Loki ingestion, shipper drops, Loki reachability.

Slice 3 of the check.py split. Reads config as `cfg.X` and every query through the `src`
argument (`bridge.sources.Sources`), so a test hands in a fake; the verdicts it from-imports
are patched on THIS module (`monkeypatch.setattr(checks.logs, "log_error_verdict", ...)`), because this is
where they are bound. Rule and enforcement: bridge/config.py's header.

`with_log_errors` lives here rather than beside `check_k8s_workloads`, its only caller,
because it is a Loki arm folded into a cluster verdict — the caller reaches it as
`checks.logs.with_log_errors` and test_check_loki.py patches it here.
"""

from collections.abc import Callable

from bridge.common import host_uptime_s
from bridge.config import Config
from bridge.sources import Sources
from bridge.types import as_object
from bridge.parsing import duration_seconds
from verdicts.cluster import log_error_verdict
from verdicts.logs import (
    kuma_notify_failures,
    loki_ingestion_fresh,
    otelcol_export_failures,
    shipper_dropped,
    swallowed_verdicts,
)


def check_loki_ingestion(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Checks that all three Loki ingestion arms (file-tail, container stream, Pi) are fresh.

    Down if any arm is silent: the cluster file-tail union, the cluster pod streams, and the
    Pi's container stream (uncounted by the other two, so a dead Pi Alloy would otherwise be
    invisible). Returns (ok, msg).
    """
    # The pod streams dwarf the file-tail streams, so arm 1 must NOT include them (else a
    # healthy pod stream masks a dead file-tail source) — hence the separate selector and the
    # wider window (LOKI_FILETAIL_WINDOW). Arms 1 and 2 also exclude every daniel-pi stream,
    # for the same masking reason (#3739). The selectors' reasoning is at bridge/config_io.py.
    ok_all, msg_all = loki_ingestion_fresh(
        src.loki_count(cfg.LOKI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_all:
        return False, "file-tail streams silent — " + msg_all
    ok_docker, msg_docker = loki_ingestion_fresh(
        src.loki_count(cfg.LOKI_DOCKER_STREAM, cfg.LOKI_WINDOW),
        cfg.LOKI_WINDOW,
    )
    if not ok_docker:
        return False, "container log stream silent — " + msg_docker
    # Arm 3: the Pi ships its own logs and neither arm above counts them, so its Alloy
    # dying is invisible while the cluster keeps talking.
    ok_pi, msg_pi = loki_ingestion_fresh(
        src.loki_count(cfg.LOKI_PI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_pi:
        return False, "daniel-pi log stream silent — " + msg_pi
    return True, "%s (+ container stream, + pi)" % msg_all


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
            return with_export_failures(
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
    return with_export_failures(cfg, src, ok, msg)


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


def check_loki_reachable(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Is Loki itself reachable and answering queries? The gate for the LOKI_DEPENDENT checks.

    Hits the labels endpoint, a fixed query independent of ingestion that returns
    status=success whenever Loki is up. That separates "Loki is down" (one root cause, one page:
    Loki Reachable) from "Loki is up but a shipper stopped shipping" (Loki Log Ingestion, which
    still evaluates whenever Loki is reachable). A raise reaches `_evaluate`, which renders the
    Loki Reachable monitor down.
    """
    envelope = as_object(
        src.get_json(cfg.LOKI_URL + "/loki/api/v1/labels"), "loki response"
    )
    if envelope.get("status") != "success":
        raise RuntimeError("loki labels status=%s" % envelope.get("status"))
    return True, "Loki reachable"


def with_log_errors(cfg: Config, src: Sources, ok: bool, msg: str) -> tuple[bool, str]:
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
# The one pusher that is a pod, not a host cron: pi-peer-backup's CronJob container (#1943).
# It has no `logger`, so it echoes its lines in the syslog shape above and Alloy lands them
# under the pod labels (`job="k8s"`, `container="pi-peer-backup"`), where the selector above
# cannot see them. A second, narrow selector rather than `job=~"syslog|k8s"`: the line cap
# below was sized against the syslog stream alone, and every pod log in the cluster would
# count toward it. The container name is the CronJob's (`pi-peer-backup/templates/
# cronjob.yaml.j2`), and it is the workload's own name rather than a generic one: `container`
# is the only label here that ties the stream to that pod, so any other workload naming a
# container the same would be read as this pusher (#1976).
SWALLOWED_VERDICTS_POD_LOGQL = '{container="pi-peer-backup"} |~ `: (status=(up|down)|push failed \\()` != "push failed transiently"'
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

    FAILS OPEN on a fetch error, on top of being in LOKI_DEPENDENT. The gate probes
    `/loki/api/v1/labels`, which answers fast while a range query is the thing a busy Loki
    is slow at, so a raise here would page this tile for a slow Loki rather than a lost
    verdict. The tile's heartbeat deadline is still the backstop for the cycle this skips.
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
            "swallowed-verdict scan unavailable (%s) — Loki Reachable owns a Loki fault"
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


def check_kuma_notify_failures(cfg: Config, src: Sources) -> tuple[bool, str]:
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
        lines = src.loki_lines(
            KUMA_NOTIFY_FAILURES_LOGQL, window_s, KUMA_NOTIFY_FAILURES_LIMIT
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
