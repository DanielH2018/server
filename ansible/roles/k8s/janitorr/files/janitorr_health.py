#!/usr/bin/env python3
"""janitorr health reader — the cluster-side replacement for monitor-bridge's check_janitorr.

Reads the running janitorr pod and its log, then pushes a verdict to the "Janitorr Errors" Kuma
push monitor. It never exits nonzero on a bad verdict: a DOWN is data to report, not a crash.

Runs as the janitorr-health CronJob (templates/health-cronjob.yaml.j2), in the repo's pinned
python image, with kubectl copied in by an init container and the pod's own ServiceAccount
(templates/health-rbac.yaml.j2) as its credential. host_lib.py and kuma_push.py sit beside it
in the same ConfigMap. The push URL, which carries the token, reaches it as KUMA_PUSH_URL from
the janitorr-health Secret through `secretKeyRef`, so the token is never in the ConfigMap or in
this file.

Usage:

    python3 /config/janitorr_health.py

Takes no arguments. `-h` and `--help` print this text and exit 0. With KUMA_PUSH_URL empty,
or naming no `/api/push/<token>` path, it pushes nothing and prints one tab-separated line,
`up<TAB>msg` or `down<TAB>msg`, which is how a hand run reads the verdict.

Environment (all optional):

    JANITORR_NAMESPACE           namespace of the janitorr Deployment (default homelab)
    JANITORR_WINDOW_H            hours of pod log scanned for errors (default 12)
    JANITORR_STARTUP_GRACE_S     seconds after a pod start before errors count (default 600)
    JANITORR_KUBECTL             kubectl command line (default "k3s kubectl")
    JANITORR_KUBECTL_TIMEOUT_S   seconds allowed per kubectl call (default 30)
    KUMA_PUSH_URL                the Janitorr Errors push monitor's URL, `<base>/api/push/<token>`
"""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
import janitorr_health_logic as logic

NAMESPACE = os.environ.get("JANITORR_NAMESPACE", "homelab")
WINDOW_S = float(os.environ.get("JANITORR_WINDOW_H", "12")) * 3600
GRACE_S = float(os.environ.get("JANITORR_STARTUP_GRACE_S", "600"))
KUBECTL = os.environ.get("JANITORR_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("JANITORR_KUBECTL_TIMEOUT_S", "30"))
# The pod log is the evidence, in the `<ts> <host> <tag>: <message>` shape rsyslog gave the host
# script's `logger -t` lines (the H2 plan's *Evidence goes to stdout*). monitor-bridge's
# Swallowed Push Verdicts check parses exactly two tokens before the tag, so a bare
# `<tag>: <message>` line would match its LogQL and never count (#5569).
TAG = "janitorr-health"


KUBECTL_RUNNER = host_lib.kubectl_runner(KUBECTL, NAMESPACE, TIMEOUT)


def emit(line: str) -> None:
    """Prints one line in the syslog shape, the pod name as host, flushed past the pipe buffer."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("%s %s %s: %s" % (stamp, socket.gethostname(), TAG, line), flush=True)


def uptime_seconds(started_at: str, now: float) -> float:
    """Seconds from an RFC 3339 timestamp to `now`. Kubernetes always emits UTC with a Z."""
    started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return now - started.timestamp()


def verdict(kubectl: Callable[..., tuple[int, str]], now: float) -> tuple[bool, str]:
    """Reads the running janitorr pod's error count and returns `(ok, msg)`.

    Every failure path it can name (unreadable pod, unparseable start time, unreadable logs)
    returns a DOWN verdict rather than raising.

    Args:
        kubectl: `kubectl(*args) -> (rc, output)`, as `host_lib.kubectl_runner` returns.
        now: the current time as a Unix timestamp, which the container's uptime is measured to.
    """
    # The CONTAINER's startedAt, not the pod's: a container that crash-looped inside a long-lived
    # pod restarts its own clock, and the boot race restarts with it. Using the pod's start time
    # would put a just-restarted janitorr past grace and count its startup errors against it.
    rc, out = kubectl(
        "get",
        "pod",
        "-l",
        "app=janitorr",
        "--field-selector",
        "status.phase=Running",
        "-o",
        "jsonpath={.items[0].status.containerStatuses[0].state.running.startedAt}",
    )
    if rc != 0:
        return False, "cannot read the janitorr pod in %s: %s" % (
            NAMESPACE,
            out.strip()[:200],
        )

    started_at = out.strip()
    if not started_at:
        return logic.janitorr_errors_ok(None, None, WINDOW_S, GRACE_S)

    try:
        uptime_s = uptime_seconds(started_at, now)
    except ValueError as e:
        return False, "unparseable container start time %r: %s" % (started_at, e)

    count = None
    if uptime_s > GRACE_S:
        window = logic.effective_window_s(uptime_s, WINDOW_S, GRACE_S)
        log_rc, logs = kubectl("logs", "deploy/janitorr", "--since=%ds" % int(window))
        if log_rc != 0:
            return False, "cannot read janitorr logs: %s" % logs.strip()[:200]
        count = sum(1 for line in logs.splitlines() if logic.ERROR_MATCH in line)

    return logic.janitorr_errors_ok(count, uptime_s, WINDOW_S, GRACE_S)


def main(
    kubectl: Callable[..., tuple[int, str]] = KUBECTL_RUNNER,
    push: Callable[..., bool] = host_lib.kuma_push,
    now: Callable[[], float] = time.time,
) -> int:
    """Pushes the verdict to Kuma, or prints it as `up\\tmsg` / `down\\tmsg` with no token.

    Always exits 0. A reader that raises pushes a DOWN naming the exception, which is what
    the host script's wrapper did for a reader that crashed: an absent push would leave the
    tile to time out with no explanation.

    Args:
        kubectl: the kubectl transport; a test passes a recorder.
        push: `kuma_push`'s signature; a test passes a stub.
        now: the clock the container's uptime is measured on.
    """
    try:
        ok, msg = verdict(kubectl, now())
    except Exception as e:  # any crash is reported as the verdict
        ok, msg = False, "janitorr health reader failed: %s: %s" % (type(e).__name__, e)
    status = "up" if ok else "down"
    # One URL rather than a base and a token, so the Secret's value is not a whole-scalar
    # placeholder: one would join k8s/manifests' standalone guard map, and a change to that
    # map fans a landing's narrowed apply out to every role that includes k8s/manifests.
    base, _, token = os.environ.get("KUMA_PUSH_URL", "").partition("/api/push/")
    if not base or not token:
        print("%s\t%s" % (status, msg))
        return 0
    emit("status=%s %s" % (status, msg))
    if push(status, msg, base, token, log=emit):
        emit("pushed")
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
