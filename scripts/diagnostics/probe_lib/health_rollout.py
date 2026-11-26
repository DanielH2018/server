"""The rollout half of `probe.py health` — a Deployment or DaemonSet's verdict.

`format_k8s_health` is pure: it takes two parsed kubectl documents and a `now`, and returns
what to print plus an exit code.

WHAT "NOT FOUND" IS ALLOWED TO MEAN governs the message for an absent workload here. The
canonical statement of that rule is health.py's module docstring; `format_k8s_health`'s own
"no Deployment or DaemonSet in this namespace" is on the MAY-SKIP side of it, because the name
it was asked about was guessed from a deploy tag rather than resolved from rendered manifests.
Read that paragraph before rewording anything below.
"""

from datetime import datetime

# The probe takes one sample, so it reads how long ago the last restart was; wider than the 60s
# Ansible soak since a sample can land anywhere in a crash cycle.
RECENT_RESTART_SECONDS = 180


def seconds_since(timestamp, now):
    """Seconds between an RFC3339 kubectl timestamp and `now`, or None if unparseable."""
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError, TypeError:
        return None
    if when.tzinfo is None:
        return None
    return (now - when).total_seconds()


def _rollout_counts(workload):
    """(desired, updated, ready, available) for a Deployment or a DaemonSet.

    DaemonSets carry the same four numbers under different names, and `desired` is the count of
    nodes the scheduler picked rather than a spec field — so a DaemonSet pinned to one node is
    complete at 1/1, not at one-per-node.
    """
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    if workload.get("kind") == "DaemonSet":
        return (
            status.get("desiredNumberScheduled", 0),
            status.get("updatedNumberScheduled", 0),
            status.get("numberReady", 0),
            status.get("numberAvailable", 0),
        )
    return (
        spec.get("replicas", 1),
        status.get("updatedReplicas", 0),
        status.get("readyReplicas", 0),
        status.get("availableReplicas", 0),
    )


def _long_running_statuses(pod):
    """The statuses of a pod's regular containers and its native sidecars.

    A native sidecar (an initContainer with `restartPolicy: Always`) reports under
    `initContainerStatuses`. A plain init container is left out: its restarts are retries
    before the pod started, such as a wait-for-<dependency> gate.
    """
    sidecars = {
        c.get("name")
        for c in (pod.get("spec") or {}).get("initContainers") or []
        if c.get("restartPolicy") == "Always"
    }
    status = pod.get("status") or {}
    return (status.get("containerStatuses") or []) + [
        cs
        for cs in status.get("initContainerStatuses") or []
        if cs.get("name") in sidecars
    ]


def format_k8s_health(deploy, pods, service, now):
    """Summarize a workload's rollout + its pods' restarts. Returns (text, exit_code).

    Pure: takes the two parsed kubectl JSON documents and a `now`, returns what to print.
    `deploy` is a Deployment or a DaemonSet — six workloads here are DaemonSets (promtail,
    node-exporter, the crowdsec node agent, dri-device-plugin, ...) and a gate that silently
    could not check them would be a gate with holes exactly where the node-level agents are.

    exit_code is 0 only when the rollout is COMPLETE (the observed generation has caught up and
    every replica is updated, ready and available, and no old-ReplicaSet pod is still
    running) AND no container restarted within
    RECENT_RESTART_SECONDS. Both halves are load-bearing: readiness alone flips Available before
    a bad liveness probe starts killing the container, which produces a green deploy and a
    crashlooping workload.
    """
    if not deploy:
        return (
            f"{service}: no Deployment or DaemonSet in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )

    meta, status = deploy.get("metadata") or {}, deploy.get("status") or {}
    desired, updated, ready, available = _rollout_counts(deploy)
    # A spec edit bumps generation at once; observedGeneration lags until the controller acts, and
    # the old ReplicaSet satisfies every count meanwhile.
    stale = status.get("observedGeneration", 0) < meta.get("generation", 0)
    # Under maxSurge >= 1 an old pod can make ready, available and updated all equal desired;
    # only `status.replicas` counts old and new together. An absent count reads 0.
    old_pods = 0
    if deploy.get("kind") != "DaemonSet":
        old_pods = max(status.get("replicas", 0) - updated, 0)
    rolled_out = (
        not stale
        and updated == desired
        and ready == desired
        and available == desired
        and not old_pods
    )

    restarts, recent = 0, []
    for pod in (pods or {}).get("items") or []:
        pod_name = (pod.get("metadata") or {}).get("name", "?")
        for cs in _long_running_statuses(pod):
            count = cs.get("restartCount", 0)
            restarts += count
            if not count:
                continue
            finished = ((cs.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"
            )
            age = seconds_since(finished, now)
            where = f"{pod_name}/{cs.get('name', '?')}"
            # An unreadable restart time counts as RECENT: reading unknown as long ago would fail
            # open.
            if age is None:
                recent.append(f"{where} restarted at an unreadable time ({finished!r})")
            elif age < RECENT_RESTART_SECONDS:
                recent.append(f"{where} restarted {int(age)}s ago")

    line = f"{service}: {ready}/{desired} ready, {updated} updated, restarts={restarts}"
    if stale:
        line += " — spec changed, rollout not observed yet"
    elif not rolled_out:
        line += " — rollout incomplete"
        if old_pods:
            line += f" ({old_pods} old pod(s) still running)"
    if recent:
        line += f" — RECENT RESTART: {'; '.join(recent)}"
    return (line, 0 if rolled_out and not recent else 1)
