"""The rollout-shaped arms of check_k8s_workloads: the stall queries and both replica streak gates.

Its own module rather than more functions in `checks/cluster.py`, which sits at the 600-line
cap the module-length ratchet enforces. Same layering as its neighbours: reads config as `cfg.X`,
the fetch layer through the `fetch` parameter `check_k8s_workloads` already threads for its other
arms, and the shared streak counter as `bridge.streaks.X`.

Rule and enforcement: bridge/config.py's header.
"""

from bridge.config import Config
import bridge.streaks
from verdicts.cluster import replica_offender_names, stalled_rollout_names


def stalled_rollout_offenders(cfg: Config, fetch) -> list[tuple[dict, float]]:
    """Deployments whose UPDATED replicas are short of the desired count, with `desired` attached.

    A PromQL `<` returns the left-hand series alone, so the desired count is not in the result's
    labels and the verdict would have nothing to compare against in its message. The second query
    supplies it, and runs only when the first found something — a stall is rare, and a healthy
    cycle pays one query rather than two.
    """
    stalled = fetch(
        cfg,
        "kube_deployment_status_replicas_updated"
        " < on(namespace, deployment) kube_deployment_spec_replicas",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    if not stalled:
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
    for labels, value in stalled:
        want = desired.get((labels.get("namespace"), labels.get("deployment")))
        named = dict(labels)
        if want is not None:
            named["desired"] = "%d" % int(want)
        out.append((named, value))
    return out


def held_replica_offenders(
    cfg: Config, offenders: list[tuple[dict, float]]
) -> tuple[list[tuple[dict, float]], str]:
    """Hold unavailable-replica offenders back until they persist K8S_WORKLOADS_CONSECUTIVE cycles.

    Returns (offenders the verdict should judge, note). The note is empty unless the streak is
    holding, in which case it carries the sibling "down streak n/N (rollout)" text naming the
    workloads being held — a monitor that stays up while a fault accumulates has to say so, and
    the green verdict text alone ("N k8s workloads healthy") would read identical to a cycle
    with nothing rolling at all.

    The gate is on THIS ARM ALONE. A Deployment rolling has one unavailable replica by
    definition, which is what every single-cycle DOWN episode this closes named — uptime-kuma(1),
    valheim(1), radarr(1), jellyfin(1), speedtest(1), karakeep-chrome(1) over the 30 days to
    2026-09-11. The crash-loop, DaemonSet, floor and log arms keep no grace: a crash-loop is
    already a multi-cycle condition by the time `increase()` sees it, and a floor breach means
    the check is blind, which delaying helps nobody.
    """
    if not offenders:
        bridge.streaks._down_streaks["k8s_workload_replicas"] = 0
        return offenders, ""
    count, held, note = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("k8s_workload_replicas", 0),
        cfg.K8S_WORKLOADS_CONSECUTIVE,
        "unavailable replicas: %s" % replica_offender_names(offenders),
        "rollout",
    )
    bridge.streaks._down_streaks["k8s_workload_replicas"] = count
    if held:
        return [], note
    return offenders, ""


def held_stalled_offenders(
    cfg: Config, offenders: list[tuple[dict, float]]
) -> tuple[list[tuple[dict, float]], str]:
    """Hold stalled-rollout offenders back until they persist K8S_ROLLOUT_STALL_CONSECUTIVE cycles.

    Same shape and same reason as `held_replica_offenders`, against a different arm: every
    healthy rollout passes through `updated < desired`, so an ungated arm would page on each one.
    The gate is what separates "rolling" from "stuck", and 3 cycles is longer than the 300s the
    playbook's own `rollout status` waits.
    """
    if not offenders:
        bridge.streaks._down_streaks["k8s_rollout_stall"] = 0
        return offenders, ""
    count, held, note = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("k8s_rollout_stall", 0),
        cfg.K8S_ROLLOUT_STALL_CONSECUTIVE,
        "rollout not updating: %s" % stalled_rollout_names(offenders),
        "rollout stall",
    )
    bridge.streaks._down_streaks["k8s_rollout_stall"] = count
    if held:
        return [], note
    return offenders, ""
