"""Pure decision core for the janitorr health cron (janitorr_health.py).

Split from the I/O shell so it stays stdlib-only (daniel-box runs it via `uv run --no-project
--python <pin>`, host_python_version in ansible/inventory/group_vars/all.yml) and unit-testable without a
cluster.

Replaces monitor-bridge's `check_janitorr`, which read the error count from **Loki** and the uptime
from `container_start_time_seconds{name="janitorr"}` in Prometheus. Neither survives the port:
cluster pod logs do not reach Loki (its `pod` label has zero values) and that cAdvisor series is
daniel-server's Docker. In the cluster both facts come from one `kubectl` read of the pod, which is
strictly less machinery than the two-source version it replaces.

`janitorr_errors_ok` is deliberately a near-verbatim port of monitor-bridge's function of the same
name rather than an import: that one lives inside check.py, a module that configures itself from
~200 environment variables at import time and cannot be loaded on a host. The behaviour is the
contract, so it is re-tested here rather than trusted.
"""

from __future__ import annotations

# The one benign, recurring ERROR is the documented post-boot race — an @Scheduled cleanup fires
# before sonarr/radarr/jellyfin finish loading, gets a FeignException 503, and self-heals next
# cycle. The line it logs is generic and identical to a real failure, with the exception type on a
# separate line, so it CANNOT be discriminated by content. It is discriminated by TIME instead.
ERROR_MATCH = "Unexpected error occurred in scheduled task"


def effective_window_s(uptime_s: float, window_s: float, grace_s: float) -> float:
    """How far back to count, so the boot race can never be inside the window.

    Once past grace, the slice is capped at `uptime - grace` — the moment the race is over — rather
    than the full window. Without the cap, a janitorr that restarted 20 minutes ago would have its
    own startup errors counted against it for the next 12 hours.
    """
    return min(window_s, uptime_s - grace_s)


def janitorr_errors_ok(count, uptime_s, window_s, grace_s):
    """Pure: decide janitorr scheduled-task health from the post-startup error count. (ok, msg).

    - uptime None (no running pod) -> ok. A janitorr that is not running is the k8s Workload
      Health monitor's concern: it reads kube_deployment_status_replicas_unavailable, which is
      exactly the signal for a routeless workload like this one. Two monitors reporting the same
      outage is the alert storm the reachability gates exist to prevent.
    - within grace_s of startup -> ok (the documented boot-race window);
    - otherwise `down` on any scheduled-task error in the post-startup window.
    """
    if uptime_s is None:
        return True, "no running janitorr pod — error check skipped"
    if uptime_s <= grace_s:
        return True, "startup grace — up %.0fs (<= %.0fs)" % (uptime_s, grace_s)
    n = int(count or 0)
    if n:
        return (
            False,
            "%d janitorr scheduled-task error(s) in the last %.0fm — see "
            "`kubectl -n homelab logs deploy/janitorr`" % (n, window_s / 60.0),
        )
    return True, "no janitorr errors (up %.1fh)" % (uptime_s / 3600.0)
