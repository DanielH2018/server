#!/usr/bin/env python3
"""configarr health reader — the cluster-side replacement for monitor-bridge's check_configarr.

Reads the last completed configarr Job and pushes a verdict to the "Configarr Sync" Kuma push
monitor. It never exits nonzero on a bad verdict: a DOWN is data to report, not a crash.

Runs as the configarr-health CronJob (templates/health-cronjob.yaml.j2), in the repo's pinned
python image, with kubectl copied in by an init container and the pod's own ServiceAccount
(templates/health-rbac.yaml.j2) as its credential. host_lib.py and kuma_push.py sit beside it
in the same ConfigMap. The push URL, which carries the token, reaches it as KUMA_PUSH_URL from
the configarr-health Secret through `secretKeyRef`, so the token is never in the ConfigMap or in
this file.

Usage:

    python3 /config/configarr_health.py

Takes no arguments. `-h` and `--help` print this text and exit 0. With KUMA_PUSH_URL empty,
or naming no `/api/push/<token>` path, it pushes nothing and prints one tab-separated line, `up<TAB>msg` or
`down<TAB>msg`, which is how a hand run reads the verdict.

Environment (all optional):

    CONFIGARR_NAMESPACE          namespace of the CronJob (default homelab)
    CONFIGARR_CRONJOB            CronJob name, also the Job label (default configarr)
    CONFIGARR_MAX_AGE_H          hours before the last finished Job counts as stale (default 26)
    CONFIGARR_KUBECTL            kubectl command line (default "k3s kubectl")
    CONFIGARR_KUBECTL_TIMEOUT_S  seconds allowed per kubectl call (default 30)
    KUMA_PUSH_URL                the Configarr Sync push monitor's URL, `<base>/api/push/<token>`
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from configarr_lib import configarr_health_logic as logic
import host_lib

NAMESPACE = os.environ.get("CONFIGARR_NAMESPACE", "homelab")
CRONJOB = os.environ.get("CONFIGARR_CRONJOB", "configarr")
MAX_AGE_S = float(os.environ.get("CONFIGARR_MAX_AGE_H", "26")) * 3600
KUBECTL = os.environ.get("CONFIGARR_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("CONFIGARR_KUBECTL_TIMEOUT_S", "30"))
# The pod log is the evidence, in the `<ts> <host> <tag>: <message>` shape rsyslog gave the host
# script's `logger -t` lines (the H2 plan's *Evidence goes to stdout*). monitor-bridge's
# Swallowed Push Verdicts check parses exactly two tokens before the tag, so a bare
# `<tag>: <message>` line would match its LogQL and never count (#5569).
TAG = "configarr-health"


KUBECTL_RUNNER = host_lib.kubectl_runner(KUBECTL, NAMESPACE, TIMEOUT)


def emit(line: str) -> None:
    """Prints one line in the syslog shape, the pod name as host, flushed past the pipe buffer."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("%s %s %s: %s" % (stamp, socket.gethostname(), TAG, line), flush=True)


def age_seconds(stamp: str, now: float) -> float:
    """Seconds from an RFC 3339 timestamp to `now`. Kubernetes always emits UTC with a Z."""
    finished = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return now - finished.timestamp()


def verdict(kubectl: Callable[..., tuple[int, str]], now: float) -> tuple[bool, str]:
    """Reads the latest finished configarr Job and returns `(ok, msg)`.

    Every failure path it can name (unreadable Jobs, unparseable JSON, no finished Job,
    unparseable finish time) returns a DOWN verdict rather than raising.

    Args:
        kubectl: `kubectl(*args) -> (rc, output)`, as `host_lib.kubectl_runner` returns.
        now: the current time as a Unix timestamp, which the Job's age is measured to.
    """
    rc, out = kubectl("get", "jobs", "-l", "app=%s" % CRONJOB, "-o", "json")
    if rc != 0:
        return False, "cannot read Jobs in %s: %s" % (NAMESPACE, out.strip()[:200])

    try:
        jobs = json.loads(out).get("items", [])
    except ValueError as e:
        return False, "unparseable Job list: %s" % e

    job = logic.latest_finished(jobs, CRONJOB)
    if job is None:
        return False, logic.decide(None, "", 0, MAX_AGE_S)[1]

    name = job["metadata"]["name"]
    try:
        age_s = age_seconds(logic.finished_at(job), now)
    except ValueError as e:
        return False, "unparseable finish time on Job %s: %s" % (name, e)

    # A failed Job's pod exits nonzero, so `kubectl logs` still returns its output — the read is
    # not conditional on success. An empty result is handled as its own failure in decide().
    #
    # DELIBERATELY NOT `--tail`. has_error_line() scans the whole output for an ERROR/FATAL line,
    # and that scan is the backstop for a soft failure that still exits 0 — the entire reason
    # configarr_status exists. configarr logs its ERROR per instance and then keeps going, so a
    # night with large diff reports would push an early ERROR out of any tail window and
    # evaluate(0, <tail>) would return clean. Same false-green class as the empty-logs gate.
    # The volume is bounded by activeDeadlineSeconds anyway.
    log_rc, logs = kubectl("logs", "job/%s" % name)
    return logic.decide(job, logs if log_rc == 0 else "", age_s, MAX_AGE_S)


def main(
    kubectl: Callable[..., tuple[int, str]] = KUBECTL_RUNNER,
    push: Callable[..., bool] = host_lib.kuma_push,
    now: Callable[[], float] = time.time,
) -> int:
    """Pushes the verdict to Kuma, or prints it as `up\tmsg` / `down\tmsg` with no token.

    Always exits 0. A reader that raises pushes a DOWN naming the exception, which is what
    the host script's wrapper did for a reader that crashed: an absent push would leave the
    tile to time out with no explanation. k8s/cronjob-gate counts a started container as a
    run whatever it exits, so the exit code carries no verdict either way.

    Args:
        kubectl: the kubectl transport; a test passes a recorder.
        push: `kuma_push`'s signature; a test passes a stub.
        now: the clock the Job's age is measured on.
    """
    try:
        ok, msg = verdict(kubectl, now())
    except Exception as e:  # any crash is reported as the verdict
        ok, msg = (
            False,
            "configarr health reader failed: %s: %s"
            % (
                type(e).__name__,
                e,
            ),
        )
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
