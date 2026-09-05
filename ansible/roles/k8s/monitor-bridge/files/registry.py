"""The check registry: every check that exists, its Kuma push token and its body.

`build_checks(env)` is the whole module. It takes the environment as a PARAMETER rather than
reading `os.environ` at import, which is what lets `main(argv, env={...})` decide which monitor
a result is pushed to — the `DECIDED` note in `cli.py` that used to say otherwise moved with the
list. The push tokens were the last configuration monitor-bridge read from a module global.

This module is a LEAF: it imports `bridge.types` for the `Check` type and the `checks.*` bodies,
and never `check` or `cli`. `ansible/roles/k8s/monitor-bridge/tests/test_check.py` reads the
`KUMA_PUSH_*` literals below as text and asserts they are exactly the set
`templates/env-secret.yaml.j2` renders, so a token spelled anywhere but here is a token no
env-secret entry backs.
"""

import os
from collections.abc import Mapping

from bridge.types import Check
from checks.notify import (
    check_discord,
)
from checks.service import (
    check_arr_queue,
    check_bazarr,
    check_etcd_restore_drill,
    check_gitops_alive,
    check_gitops_status,
    check_staging_backfill_alive,
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
    check_traefik_5xx,
    check_traefik_latency,
)
from checks.host import check_cert, check_disk, check_mem
from checks.host_thermal import check_host_temp, check_scrutiny, check_ups
from checks.host_edge import check_pi_pressure, check_speedtest
from checks.b2 import (
    check_b2_storage,
)
from checks.r2 import check_r2_usage
from checks.storage import (
    check_kubelet_plugin_readonly,
    check_longhorn_volumes,
    check_pvc_fullness,
)
from checks.logs import (
    check_loki_ingestion,
    check_shipper_dropped,
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
        return e.get(name, "")

    return [
        Check("disk", tok("KUMA_PUSH_DISK"), check_disk),
        Check("cert", tok("KUMA_PUSH_CERT"), check_cert),
        Check("memory", tok("KUMA_PUSH_MEM"), check_mem),
        # restarts/oom/cpu RETARGETED 2026-08-14 (Phase G): retired with the Docker cadvisor
        # the same morning, re-armed the same evening against the kubernetes-cadvisor job's
        # label shape — grouped by pod (`name` is the runtime hash there). Same pure logic,
        # same thresholds; complements k8s_workloads' crashloop paging with OOM + sustained-
        # throttle depth the retirement dropped.
        Check("restarts", tok("KUMA_PUSH_RESTARTS"), check_restarts),
        Check("oom", tok("KUMA_PUSH_OOM"), check_oom),
        Check("cpu", tok("KUMA_PUSH_CPU"), check_cpu_throttle),
        Check("targets", tok("KUMA_PUSH_TARGETS"), check_targets_down),
        Check("traefik5xx", tok("KUMA_PUSH_TRAEFIK"), check_traefik_5xx),
        Check(
            "traefik_latency",
            tok("KUMA_PUSH_TRAEFIK_LATENCY"),
            check_traefik_latency,
        ),
        Check("n8n", tok("KUMA_PUSH_N8N"), check_n8n),
        Check("arr_queue", tok("KUMA_PUSH_ARR_QUEUE"), check_arr_queue),
        Check("bazarr", tok("KUMA_PUSH_BAZARR"), check_bazarr),
        Check(
            "prowlarr_indexers",
            tok("KUMA_PUSH_PROWLARR_INDEXERS"),
            check_prowlarr_indexers,
        ),
        Check("gitops_alive", tok("KUMA_PUSH_GITOPS_ALIVE"), check_gitops_alive),
        Check("gitops_status", tok("KUMA_PUSH_GITOPS_STATUS"), check_gitops_status),
        # The staging-gate backfill ratchet's run-recency arm. Same shape and same hostPath as
        # the gitops pair above — it reads the unit's heartbeat out of /var/lib/gitops-deploy.
        # Its sibling `OnFailure=` unit pages when a run FAILS; this pages when runs stop
        # happening.
        Check(
            "staging_backfill",
            tok("KUMA_PUSH_STAGING_BACKFILL"),
            check_staging_backfill_alive,
        ),
        # Reads a stamp the drill writes weekly rather than a live source, so it is the same
        # shape as the gitops pair above: a hostPath the pod is pinned to, read fail-closed. Its
        # token was minted 2026-08-28, which is what let it be registered —
        # test_checks_and_env_secret_push_tokens_agree blocks a check whose KUMA_PUSH_* name has
        # no env-secret entry, correctly: such a check pushes to nowhere forever, present in the
        # code and absent from the world.
        Check(
            "etcd_restore_drill",
            tok("KUMA_PUSH_ETCD_DRILL"),
            check_etcd_restore_drill,
        ),
        Check("scrutiny", tok("KUMA_PUSH_SCRUTINY"), check_scrutiny),
        Check("host_temp", tok("KUMA_PUSH_HOST_TEMP"), check_host_temp),
        Check("ups", tok("KUMA_PUSH_UPS"), check_ups),
        Check("pi_pressure", tok("KUMA_PUSH_PI"), check_pi_pressure),
        Check("ha_heartbeat", tok("KUMA_PUSH_HA"), check_ha_heartbeat),
        Check("speedtest", tok("KUMA_PUSH_SPEEDTEST"), check_speedtest),
        Check("loki_ingestion", tok("KUMA_PUSH_LOKI"), check_loki_ingestion),
        Check(
            "shipper_dropped",
            tok("KUMA_PUSH_SHIPPER_DROPPED"),
            check_shipper_dropped,
        ),
        Check("discord", tok("KUMA_PUSH_DISCORD"), check_discord),
        Check("r2_usage", tok("KUMA_PUSH_R2_USAGE"), check_r2_usage),
        Check("b2_storage", tok("KUMA_PUSH_B2_STORAGE"), check_b2_storage),
        Check("k8s_workloads", tok("KUMA_PUSH_K8S_WORKLOADS"), check_k8s_workloads),
        Check(
            "cluster_targets", tok("KUMA_PUSH_CLUSTER_TARGETS"), check_cluster_targets
        ),
        Check(
            "longhorn_volumes",
            tok("KUMA_PUSH_LONGHORN_VOLUMES"),
            check_longhorn_volumes,
        ),
        Check("pvc_fullness", tok("KUMA_PUSH_PVC"), check_pvc_fullness),
        Check(
            "kubelet_plugin_readonly",
            tok("KUMA_PUSH_KUBELET_READONLY"),
            check_kubelet_plugin_readonly,
        ),
    ]
