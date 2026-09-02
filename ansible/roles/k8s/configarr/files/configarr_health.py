#!/usr/bin/env python3
"""configarr health reader — the cluster-side replacement for monitor-bridge's check_configarr.

Reads the last completed configarr Job through the read-only ServiceAccount kubeconfig and prints
one tab-separated line, `up<TAB>msg` or `down<TAB>msg`, for the wrapper to push to Kuma. It never
exits nonzero on a bad verdict: a DOWN is data to report, not a crash, and the wrapper needs the
message either way.

Runs on daniel-box via `uv run --no-project --python <pin>` (host_python_version in
ansible/inventory/group_vars/all.yml). The push URL carries a token, so it stays in the
templated wrapper and never appears here; this file is plaintext in git.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configarr_health_logic as logic
import host_lib

NAMESPACE = os.environ.get("CONFIGARR_NAMESPACE", "homelab")
CRONJOB = os.environ.get("CONFIGARR_CRONJOB", "configarr")
MAX_AGE_S = float(os.environ.get("CONFIGARR_MAX_AGE_H", "26")) * 3600
KUBECTL = os.environ.get("CONFIGARR_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("CONFIGARR_KUBECTL_TIMEOUT_S", "30"))

kubectl = host_lib.kubectl_runner(KUBECTL, NAMESPACE, TIMEOUT)


def age_seconds(stamp: str) -> float:
    """Seconds since an RFC 3339 timestamp. Kubernetes always emits UTC with a trailing Z."""
    finished = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return time.time() - finished.timestamp()


def main() -> int:
    rc, out = kubectl("get", "jobs", "-l", "app=%s" % CRONJOB, "-o", "json")
    if rc != 0:
        print("down\tcannot read Jobs in %s: %s" % (NAMESPACE, out.strip()[:200]))
        return 0

    try:
        jobs = json.loads(out).get("items", [])
    except ValueError as e:
        print("down\tunparseable Job list: %s" % e)
        return 0

    job = logic.latest_finished(jobs, CRONJOB)
    if job is None:
        print("down\t%s" % logic.decide(None, "", 0, MAX_AGE_S)[1])
        return 0

    name = job["metadata"]["name"]
    try:
        age_s = age_seconds(logic.finished_at(job))
    except ValueError as e:
        print("down\tunparseable finish time on Job %s: %s" % (name, e))
        return 0

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
    ok, msg = logic.decide(job, logs if log_rc == 0 else "", age_s, MAX_AGE_S)
    print("%s\t%s" % ("up" if ok else "down", msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
