"""The rollout half of `probe.py health` — a Deployment or DaemonSet's verdict.

Split out of probe_lib/health.py, which had grown to 938 lines. `format_k8s_health` is pure:
it takes two parsed kubectl documents and a `now`, and returns what to print plus an exit code.

WHAT "NOT FOUND" IS ALLOWED TO MEAN governs the message for an absent workload here. The
canonical statement of that rule is health.py's module docstring; `format_k8s_health`'s own
"no Deployment or DaemonSet in this namespace" is on the MAY-SKIP side of it, because the name
it was asked about was guessed from a deploy tag rather than resolved from rendered manifests.
Read that paragraph before rewording anything below.
"""

from datetime import datetime, timezone

# A container that crashlooped this recently is not healthy, however ready it reads right now.
# Ansible's post-rollout gate soaks for 60s (k8s_rollout_stabilise_seconds) watching the restart
# COUNT, because a pod that crashes and recovers within a second passes every readiness-derived
# field — see roles/k8s/manifests/tasks/assert_stable.yml. probe.py takes ONE sample instead of
# soaking, so it reads the same signal from the other end: how long ago the last restart was.
# Wider than the Ansible window, since a single sample can land anywhere in a crash cycle.
RECENT_RESTART_SECONDS = 180


def _seconds_since(timestamp, now):
    """Seconds between an RFC3339 kubectl timestamp and `now`, or None if unparseable."""
    if not timestamp:
        return None
    try:
        when = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError, TypeError:
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


def format_k8s_health(deploy, pods, service, now):
    """Summarize a workload's rollout + its pods' restarts. Returns (text, exit_code).

    Pure: takes the two parsed kubectl JSON documents and a `now`, returns what to print.
    `deploy` is a Deployment or a DaemonSet — six workloads here are DaemonSets (promtail,
    node-exporter, the crowdsec node agent, dri-device-plugin, ...) and a gate that silently
    could not check them would be a gate with holes exactly where the node-level agents are.

    exit_code is 0 only when the rollout is COMPLETE (the observed generation has caught up and
    every replica is updated, ready and available) AND no container restarted within
    RECENT_RESTART_SECONDS. Both halves are load-bearing: readiness alone flips Available before
    a bad liveness probe starts killing the container, which is the failure that produced a green
    deploy and a crashlooping kube-state-metrics on 2026-08-07.
    """
    if not deploy:
        return (
            f"{service}: no Deployment or DaemonSet in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )

    meta, status = deploy.get("metadata") or {}, deploy.get("status") or {}
    desired, updated, ready, available = _rollout_counts(deploy)
    # A spec edit bumps metadata.generation immediately; status.observedGeneration only catches
    # up once the controller has acted. Comparing them is what distinguishes "rolled out" from
    # "the controller has not looked at my change yet" — the old ReplicaSet satisfies every
    # replica count in the meantime.
    stale = status.get("observedGeneration", 0) < meta.get("generation", 0)
    rolled_out = (
        not stale and updated == desired and ready == desired and available == desired
    )

    restarts, recent = 0, []
    for pod in (pods or {}).get("items") or []:
        pod_name = (pod.get("metadata") or {}).get("name", "?")
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            count = cs.get("restartCount", 0)
            restarts += count
            if not count:
                continue
            finished = ((cs.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"
            )
            age = _seconds_since(finished, now)
            where = f"{pod_name}/{cs.get('name', '?')}"
            # A restart whose time cannot be read counts as RECENT. Treating "unknown" as "long
            # ago" would fail open — the one direction a gate must never fail — and this branch
            # is reachable whenever kubectl's timestamp format shifts under us (every finishedAt
            # in this cluster is second-precision UTC today; fractional seconds parse as None).
            if age is None:
                recent.append(f"{where} restarted at an unreadable time ({finished!r})")
            elif age < RECENT_RESTART_SECONDS:
                recent.append(f"{where} restarted {int(age)}s ago")

    line = f"{service}: {ready}/{desired} ready, {updated} updated, restarts={restarts}"
    if stale:
        line += " — spec changed, rollout not observed yet"
    elif not rolled_out:
        line += " — rollout incomplete"
    if recent:
        line += f" — RECENT RESTART: {'; '.join(recent)}"
    return (line, 0 if rolled_out and not recent else 1)
