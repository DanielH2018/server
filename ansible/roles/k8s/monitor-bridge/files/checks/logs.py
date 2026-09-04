"""Log-pipeline checks for monitor-bridge — Loki ingestion, shipper drops, Loki reachability.

Slice 3 of the check.py split. Reads config as `cfg.X` and the fetch layer as `bridge.net.X`,
so the tests' patches on those modules reach it; the verdicts it from-imports are patched on
THIS module (`monkeypatch.setattr(checks.logs, "log_error_verdict", ...)`), because this is
where they are bound. Rule and enforcement: bridge/config.py's header.

`with_log_errors` lives here rather than beside `check_k8s_workloads`, its only caller,
because it is a Loki arm folded into a cluster verdict — the caller reaches it as
`checks.logs.with_log_errors` and test_check_loki.py patches it here.
"""

import bridge.config as cfg
import bridge.net
from verdicts.cluster import log_error_verdict
from verdicts.logs import loki_ingestion_fresh, shipper_dropped


def check_loki_ingestion() -> tuple[bool, str]:
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
        bridge.net.loki_count(cfg.LOKI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_all:
        return False, "file-tail streams silent — " + msg_all
    ok_docker, msg_docker = loki_ingestion_fresh(
        bridge.net.loki_count(cfg.LOKI_DOCKER_STREAM, cfg.LOKI_WINDOW), cfg.LOKI_WINDOW
    )
    if not ok_docker:
        return False, "container log stream silent — " + msg_docker
    # Arm 3: the Pi ships its own logs and neither arm above counts them, so its promtail
    # dying is invisible while the cluster keeps talking.
    ok_pi, msg_pi = loki_ingestion_fresh(
        bridge.net.loki_count(cfg.LOKI_PI_STREAM, cfg.LOKI_FILETAIL_WINDOW),
        cfg.LOKI_FILETAIL_WINDOW,
    )
    if not ok_pi:
        return False, "daniel-pi log stream silent — " + msg_pi
    return True, "%s (+ container stream, + pi)" % msg_all


def check_shipper_dropped() -> tuple[bool, str]:
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
        'sum(increase({__name__=~"%s"}[%s]))'
        % (cfg.SHIPPER_DROPPED_METRICS, cfg.SHIPPER_DROPPED_WINDOW)
    )
    server_reasons = [
        (labels.get("reason", "unknown"), value)
        for labels, value in bridge.net.prom_vector(
            'sum by (reason) (increase({__name__=~"%s"}[%s]))'
            % (cfg.SHIPPER_DROPPED_SERVER_METRIC, cfg.SHIPPER_DROPPED_WINDOW)
        )
    ]
    return shipper_dropped(
        client_count,
        server_reasons,
        cfg.SHIPPER_DROPPED_WINDOW,
        cfg.SHIPPER_DROPPED_MAX,
    )


def check_loki_reachable() -> tuple[bool, str]:
    bridge.net.loki_reachable()
    return True, "Loki reachable"


def with_log_errors(ok: bool, msg: str) -> tuple[bool, str]:
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
