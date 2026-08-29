#!/usr/bin/env python3
"""janitorr health reader — the cluster-side replacement for monitor-bridge's check_janitorr.

Reads the running janitorr pod through the read-only ServiceAccount kubeconfig and prints one
tab-separated line, `up<TAB>msg` or `down<TAB>msg`, for the wrapper to push to Kuma. It never exits
nonzero on a bad verdict: a DOWN is data to report, not a crash, and the wrapper needs the message
either way.

Runs on daniel-box via `uv run --no-project --python <pin>` (host_python_version in
ansible/inventory/group_vars/all.yml). The push URL carries a token, so it stays in the
templated wrapper and never appears here; this file is plaintext in git.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib  # noqa: E402  (sibling copy, deployed alongside — see the k8s/janitorr role)
import janitorr_health_logic as logic  # noqa: E402  (sibling module, via the sys.path insert)

NAMESPACE = os.environ.get("JANITORR_NAMESPACE", "homelab")
WINDOW_S = float(os.environ.get("JANITORR_WINDOW_H", "12")) * 3600
GRACE_S = float(os.environ.get("JANITORR_STARTUP_GRACE_S", "600"))
KUBECTL = os.environ.get("JANITORR_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("JANITORR_KUBECTL_TIMEOUT_S", "30"))


kubectl = host_lib.kubectl_runner(KUBECTL, NAMESPACE, TIMEOUT)


def uptime_seconds(started_at: str) -> float:
    """Seconds since an RFC 3339 timestamp. Kubernetes always emits UTC with a trailing Z."""
    started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return time.time() - started.timestamp()


def main() -> int:
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
        print(
            "down\tcannot read the janitorr pod in %s: %s"
            % (NAMESPACE, out.strip()[:200])
        )
        return 0

    started_at = out.strip()
    if not started_at:
        ok, msg = logic.janitorr_errors_ok(None, None, WINDOW_S, GRACE_S)
        print("%s\t%s" % ("up" if ok else "down", msg))
        return 0

    try:
        uptime_s = uptime_seconds(started_at)
    except ValueError as e:
        print("down\tunparseable container start time %r: %s" % (started_at, e))
        return 0

    count = None
    if uptime_s > GRACE_S:
        window = logic.effective_window_s(uptime_s, WINDOW_S, GRACE_S)
        log_rc, logs = kubectl("logs", "deploy/janitorr", "--since=%ds" % int(window))
        if log_rc != 0:
            print("down\tcannot read janitorr logs: %s" % logs.strip()[:200])
            return 0
        count = sum(1 for line in logs.splitlines() if logic.ERROR_MATCH in line)

    ok, msg = logic.janitorr_errors_ok(count, uptime_s, WINDOW_S, GRACE_S)
    print("%s\t%s" % ("up" if ok else "down", msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
