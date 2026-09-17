"""The zero-available arm of check_k8s_workloads: its Prometheus query and its streak gate.

A Deployment with NO available replicas is down, not rolling, and the unavailable-replica arm's
K8S_WORKLOADS_CONSECUTIVE grace (15 min) treats the two alike (#1802). This arm reads the same
Deployments through `available == 0` and holds them for K8S_ZERO_AVAILABLE_CONSECUTIVE cycles
instead — shorter, because the only things that legitimately sit at zero are a Recreate swap
and a cold start, and neither takes ten minutes.

Its own module for the same reason `cluster_rollout.py` is one: `checks/cluster.py` sits at
the 600-line cap the module-length ratchet enforces. Same layering — config as `cfg.X`, the
fetch layer through the `fetch` parameter `check_k8s_workloads` threads to its other arms, the
shared streak counter as `bridge.streaks.X`.
"""

from bridge.config import Config
import bridge.streaks
from verdicts.cluster import stalled_rollout_names


def zero_available_offenders(cfg: Config, fetch) -> list[tuple[dict, float]]:
    """Deployments with zero available replicas while wanting at least one, `desired` attached.

    `and on(...)` keeps the left-hand series (the zero) and drops a Deployment scaled to zero on
    purpose. The desired count is fetched separately for the message, exactly as the stalled
    arm does: a bare `authelia(0)` reads as "zero replicas", which is the scaled-down case this
    query already excludes, where `authelia(0/1)` says what is wrong.
    """
    zero = fetch(
        cfg,
        "kube_deployment_status_replicas_available == 0"
        " and on(namespace, deployment) kube_deployment_spec_replicas > 0",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    if not zero:
        return []
    desired = {
        (labels.get("namespace"), labels.get("deployment")): value
        for labels, value in fetch(
            cfg,
            "kube_deployment_spec_replicas",
            base=cfg.CLUSTER_PROM_URL,
            source="cluster prometheus",
        )
    }
    out = []
    for labels, value in zero:
        want = desired.get((labels.get("namespace"), labels.get("deployment")))
        named = dict(labels)
        if want is not None:
            named["desired"] = "%d" % int(want)
        out.append((named, value))
    return out


def held_zero_available_offenders(
    cfg: Config, offenders: list[tuple[dict, float]]
) -> tuple[list[tuple[dict, float]], str]:
    """Hold zero-available offenders back until they persist K8S_ZERO_AVAILABLE_CONSECUTIVE cycles.

    The grace is shorter than the unavailable-replica arm's and separate from it: a Deployment
    at zero is in both offender sets, and the streaks advance independently, so this arm pages
    first and the other one's later page names the same workload. The note carries a
    `cold start` label rather than `rollout`, because a Recreate swap and a node's cold start
    are what this grace exists for.
    """
    if not offenders:
        bridge.streaks._down_streaks["k8s_zero_available"] = 0
        return offenders, ""
    count, held, note = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("k8s_zero_available", 0),
        cfg.K8S_ZERO_AVAILABLE_CONSECUTIVE,
        "no available replicas: %s" % stalled_rollout_names(offenders),
        "cold start",
    )
    bridge.streaks._down_streaks["k8s_zero_available"] = count
    if held:
        return [], note
    return offenders, ""
