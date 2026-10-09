"""The check registry: every check that exists, its Kuma push token and its body.

`build_checks(env)` is the whole module. It takes the environment as a PARAMETER rather than
reading `os.environ` at import, which is what lets `main(argv, env={...})` decide which monitor
a result is pushed to — the `DECIDED` note in `cli.py` that used to say otherwise moved with the
list. The push tokens were the last configuration monitor-bridge read from a module global.

This module is a LEAF: it imports `bridge.types` for the `Check` type and the `checks.*` bodies,
and never `check` or `cli`. Each check's token is read from `bridge.types.push_env(name)`, the
`KUMA_PUSH_<NAME>` the env-secret renders for the row of the same name in
`monitor_bridge_push_checks` (`defaults/main.yml`). `tests/test_push_check_table.py` holds the
registry's names and the table's equal, so a check with no row is a check pushing nowhere.
"""

import os
from collections.abc import Mapping

from bridge.types import Check, push_env
from checks.notify import (
    check_discord,
)
from checks.gitops import check_gitops_alive, check_gitops_status
from checks.service import (
    check_arr_queue,
    check_bazarr,
    check_etcd_restore_drill,
    check_ha_heartbeat,
    check_n8n,
    check_prowlarr_indexers,
)
from checks.cluster import (
    check_cluster_targets,
    check_cpu_throttle,
    check_k8s_workloads,
    check_oom,
    check_restarts,
    check_targets_down,
    check_traefik_404_flood,
    check_traefik_5xx,
    check_traefik_latency,
)
from checks.cluster_etcd import check_etcd_db_size
from checks.cluster_traefik import check_traefik_421
from checks.host import check_cert, check_disk, check_mem
from checks.host_thermal import check_host_temp, check_scrutiny, check_ups
from checks.host_edge import check_pi_pressure, check_speedtest
from checks.b2 import (
    check_b2_storage,
)
from checks.r2 import check_r2_usage
from checks.cloudflare_ips import check_cloudflare_ips_drift
from checks.healthchecks import check_healthchecks_drift
from checks.storage import (
    check_kubelet_plugin_readonly,
    check_longhorn_volumes,
    check_pvc_fullness,
    check_snapshot_headroom,
)
from checks.logs import (
    check_kuma_notify_failures,
    check_loki_ingestion,
    check_shipper_dropped,
    check_swallowed_verdicts,
)


def build_checks(env: Mapping[str, str] | None = None) -> list[Check]:
    """Every check, in evaluation order, with its push token read from `env`.

    Args:
      env: The environment the `KUMA_PUSH_*` tokens are read from. None reads `os.environ`,
        which is what the pod does.

    Returns:
      A fresh list — the caller owns it, so a test can hand `run_once` a different one without
      mutating anything shared.
    """
    e = os.environ if env is None else env

    def tok(name: str) -> str:
        return e.get(push_env(name), "")

    return [
        Check("disk", tok("disk"), check_disk),
        Check("cert", tok("cert"), check_cert),
        Check("memory", tok("memory"), check_mem),
        # restarts/oom/cpu RETARGETED 2026-08-14 (Phase G): retired with the Docker cadvisor
        # the same morning, re-armed the same evening against the kubernetes-cadvisor job's
        # label shape — grouped by pod (`name` is the runtime hash there). Same pure logic,
        # same thresholds; complements k8s_workloads' crashloop paging with OOM + sustained-
        # throttle depth the retirement dropped.
        Check("restarts", tok("restarts"), check_restarts),
        Check("oom", tok("oom"), check_oom),
        Check("cpu", tok("cpu"), check_cpu_throttle),
        Check("targets", tok("targets"), check_targets_down),
        Check("traefik5xx", tok("traefik5xx"), check_traefik_5xx),
        Check(
            "traefik_latency",
            tok("traefik_latency"),
            check_traefik_latency,
        ),
        # Minted 2026-09-06 after #1322: its two neighbours above are per-SERVICE, and a
        # total-404 edge erases the traefik_service_* series they iterate, so both read green
        # through a 3.5-hour outage. This one reads the entrypoint counter, which survives it.
        Check(
            "traefik_404",
            tok("traefik_404"),
            check_traefik_404_flood,
        ),
        # Minted 2026-09-27 after #2747 and #2749: a client wedged on a connection SNICheck
        # pinned wrong gets 421 from the router and never reaches a service, so none of the
        # three Traefik checks above counts it.
        Check("traefik_421", tok("traefik_421"), check_traefik_421),
        Check("n8n", tok("n8n"), check_n8n),
        Check("arr_queue", tok("arr_queue"), check_arr_queue),
        Check("bazarr", tok("bazarr"), check_bazarr),
        Check(
            "prowlarr_indexers",
            tok("prowlarr_indexers"),
            check_prowlarr_indexers,
        ),
        Check("gitops_alive", tok("gitops_alive"), check_gitops_alive),
        Check("gitops_status", tok("gitops_status"), check_gitops_status),
        # Reads a stamp the drill writes weekly rather than a live source, so it is the same
        # shape as the gitops pair above: a hostPath the pod is pinned to, read fail-closed. Its
        # token was minted 2026-08-28, which is what let it be registered —
        # tests/test_push_check_table.py blocks a check with no `monitor_bridge_push_checks` row
        # or no SOPS secret, correctly: such a check pushes to nowhere forever, present in the
        # code and absent from the world.
        Check(
            "etcd_restore_drill",
            tok("etcd_restore_drill"),
            check_etcd_restore_drill,
        ),
        # The drill above proves a restore works; this one watches the failure a restore
        # follows. etcd's own db-size series is empty while k3s_etcd_expose_metrics is off, so
        # it reads the apiserver's unconditional proxy for the same number (#2403).
        Check(
            "etcd_db_size",
            tok("etcd_db_size"),
            check_etcd_db_size,
        ),
        Check("scrutiny", tok("scrutiny"), check_scrutiny),
        Check("host_temp", tok("host_temp"), check_host_temp),
        Check("ups", tok("ups"), check_ups),
        Check("pi_pressure", tok("pi_pressure"), check_pi_pressure),
        Check("ha_heartbeat", tok("ha_heartbeat"), check_ha_heartbeat),
        Check("speedtest", tok("speedtest"), check_speedtest),
        Check("loki_ingestion", tok("loki_ingestion"), check_loki_ingestion),
        Check(
            "shipper_dropped",
            tok("shipper_dropped"),
            check_shipper_dropped,
        ),
        # Minted 2026-09-17 for #1869: kuma-push-lib.sh logs a lost push and returns 0, so a
        # host cron's DOWN verdict that never reached its tile was reported only by that
        # tile's heartbeat deadline — a day and an hour later for the daily drift producers.
        # Reads the library's own final-failure line out of Loki, so it is Loki-dependent.
        Check(
            "swallowed_verdicts",
            tok("swallowed_verdicts"),
            check_swallowed_verdicts,
        ),
        # Minted 2026-09-17 for #1891: Kuma logs `Cannot send notification to <name>` and
        # does not retry, so that transition or resend reached nobody. check_discord GET-verifies
        # the webhook and cannot see a dropped POST. Reads Kuma's own line out of Loki, so it is
        # Loki-dependent; its tile notifies email as well as Discord.
        Check(
            "kuma_notify_failures",
            tok("kuma_notify_failures"),
            check_kuma_notify_failures,
        ),
        Check("discord", tok("discord"), check_discord),
        Check("r2_usage", tok("r2_usage"), check_r2_usage),
        Check(
            "cloudflare_ips_drift",
            tok("cloudflare_ips_drift"),
            check_cloudflare_ips_drift,
        ),
        Check(
            "healthchecks_drift",
            tok("healthchecks_drift"),
            check_healthchecks_drift,
        ),
        Check("b2_storage", tok("b2_storage"), check_b2_storage),
        Check("k8s_workloads", tok("k8s_workloads"), check_k8s_workloads),
        Check("cluster_targets", tok("cluster_targets"), check_cluster_targets),
        Check(
            "longhorn_volumes",
            tok("longhorn_volumes"),
            check_longhorn_volumes,
        ),
        Check("pvc_fullness", tok("pvc_fullness"), check_pvc_fullness),
        # The third storage axis, beside replica redundancy and claim fullness: snapshot space
        # against a capped volume's spec.snapshotMaxSize (#1627). Snapshots live in the Longhorn
        # backend, so a volume can fill its cap while the claim reads nearly empty and every
        # replica reads healthy — and a reached cap makes Longhorn refuse new snapshots rather
        # than prune, failing every later deploy of that service (#1560).
        Check(
            "snapshot_headroom",
            tok("snapshot_headroom"),
            check_snapshot_headroom,
        ),
        Check(
            "kubelet_plugin_readonly",
            tok("kubelet_plugin_readonly"),
            check_kubelet_plugin_readonly,
        ),
    ]
