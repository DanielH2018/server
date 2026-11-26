"""How the soak Job reads a pod: its owning workload, its long-running containers, its service.

`flux_soak.py` imports it, and the soak ConfigMap carries it beside that script.
"""

from collections.abc import Mapping
from typing import NamedTuple

from soak_reads import Reads

WORKLOADS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})
# A StatefulSet has no recording rule, so its unmapped pod always fails.
WORKLOAD_RULES = {
    "Deployment": ("homelab:deployment_service:info", "deployment"),
    "DaemonSet": ("homelab:daemonset_service:info", "daemonset"),
}
UNMAPPED = "unmapped"


def controller_of(obj: Mapping) -> Mapping:
    for owner in obj.get("metadata", {}).get("ownerReferences") or []:
        if owner.get("controller"):
            return owner
    return {}


class WorkloadPod(NamedTuple):
    """A pod, and the Deployment, DaemonSet or StatefulSet its owner chain ends in."""

    pod: Mapping
    kind: str
    workload: str


def samples_by_pod(result: list) -> dict[tuple, list]:
    """Each series' `(namespace, pod[, container])`, mapped to its raw `[time, value]` samples."""
    out: dict[tuple, list] = {}
    for series in result:
        metric = series["metric"]
        key = (metric.get("namespace"), metric.get("pod"))
        if "container" in metric:
            key += (metric["container"],)
        out.setdefault(key, []).extend(series.get("values") or [series.get("value")])
    return out


def sidecar_names(pod: Mapping) -> set[str]:
    return {
        c["name"]
        for c in (pod.get("spec") or {}).get("initContainers") or []
        if c.get("restartPolicy") == "Always"
    }


def long_running_statuses(pod: Mapping) -> list[Mapping]:
    """A pod's container statuses plus its native sidecars'; plain init containers retry."""
    status = pod.get("status") or {}
    sidecars = sidecar_names(pod)
    return (status.get("containerStatuses") or []) + [
        c for c in status.get("initContainerStatuses") or [] if c["name"] in sidecars
    ]


def restart_counts(pod: Mapping) -> dict[str, int]:
    return {c["name"]: c.get("restartCount", 0) for c in long_running_statuses(pod)}


def judged_pods(
    reads: Reads, pods: Mapping[tuple, WorkloadPod], services: frozenset[str]
) -> tuple[dict[tuple, str], list[tuple[str, str]]]:
    """Which of `pods` the soak judges, under which service, and which it names as advisory.

    A pod judges under its `homelab:pod_service:info` service when that is in `services`, the
    scope. A pod with no series, or one reading `unmapped`, judges as UNMAPPED and fails,
    unless its workload's recording rule names a service outside the scope: a pod recreated
    mid-soak before the rule maps it is not R's doing. Every other pod is left out.

    Returns:
        The judged pods' keys mapped to their service, and each advisory pod's
        `namespace/pod` label beside its workload's service.
    """
    by_pod = {
        (s["metric"].get("namespace"), s["metric"].get("pod")): s["metric"].get(
            "service", UNMAPPED
        )
        for s in reads.query("homelab:pod_service:info", None)
    }
    unmapped = {
        key: owned
        for key, owned in pods.items()
        if by_pod.get(key, UNMAPPED) == UNMAPPED
    }
    by_workload: dict[tuple, str] = {}
    for kind in sorted(
        {owned.kind for owned in unmapped.values()} & WORKLOAD_RULES.keys()
    ):
        rule, label = WORKLOAD_RULES[kind]
        by_workload |= {
            (kind, s["metric"].get("namespace"), s["metric"].get(label)): s[
                "metric"
            ].get("service", UNMAPPED)
            for s in reads.query(rule, None)
        }
    judged, advisory = {}, []
    for key in sorted(pods):
        service = by_pod.get(key, UNMAPPED)
        if key in unmapped:
            owned = unmapped[key]
            workload = by_workload.get((owned.kind, key[0], owned.workload), UNMAPPED)
            if workload != UNMAPPED and workload not in services:
                advisory.append(("/".join(key), workload))
            else:
                judged[key] = UNMAPPED
        elif service in services:
            judged[key] = service
    return judged, advisory
