"""Ansible filter plugin choosing this node's ready longhorn-manager pod, the Longhorn API's address.

A process in a node's host netns reaches the Longhorn HTTP API only through that node's own
longhorn-manager pod; the `longhorn-backend` ClusterIP load-balances across nodes and fails
whenever it picks the other one. Three callers resolve the local pod: `k8s/longhorn-api`'s
`resolve.yml` (attach, revert, detach), the trim cron (`setup/k3s/templates/
longhorn-trim-volumes.sh.j2`) and the snapshot reaper (`scripts/backup/
longhorn_reap_orphan_snapshots.py`). The rule is written once here (#3736). The reaper and
`resolve.yml` call this function; the trim cron runs on the host with no repo checkout, so it
keeps a jq spelling of the same rule, and a parity test runs both over the same pods.

The rule: on this node, phase `Running`, and every container ready. A pod reporting no
`containerStatuses` is NOT ready, because its containers have not started.

No Ansible import, so the reaper and the tests call the same function the playbook runs.
"""


def ready_manager_ip(pods: list, node: str) -> str:
    """The podIP of the first ready longhorn-manager pod on `node`, or "" when there is none.

    Args:
        pods: The `.items` of `kubectl get pods -l app=longhorn-manager -o json`.
        node: The node name, `ansible_hostname` or `hostname`.

    Returns:
        The first match in list order. A terminating pod's object can outlive its container for
        the grace period, so a list taken mid-eviction can carry two pods for one node; the
        readiness check drops the terminating one once its container stops.
    """
    for pod in pods or []:
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        if spec.get("nodeName") != node or status.get("phase") != "Running":
            continue
        statuses = status.get("containerStatuses") or []
        if statuses and all(cs.get("ready") for cs in statuses):
            return status.get("podIP") or ""
    return ""


class FilterModule:
    """Registers `ready_manager_ip`."""

    def filters(self):
        return {"ready_manager_ip": ready_manager_ip}
