"""Guard: the host forwarder's metrics port must admit the agent nodes, not just the pod CIDR.

WHY THE POD-CIDR ALLOW IS NOT ENOUGH. Flannel masquerades a pod's traffic to any address
outside the pod CIDR, and a node's own LAN address is outside it. So when Prometheus runs on
daniel-server, its scrape of daniel-box's `coredns-host` target arrives from daniel-server's
LAN IP, and an allow scoped to `k3s_pod_cidr` drops it. Prometheus is a single-replica
Deployment with no nodeSelector, so which node it runs on is the scheduler's choice after
every reboot.

WHY IT NEEDS A GUARD. The failure is a red Scrape Targets monitor and nothing else: the pod is
Ready, the host process answers locally, and the deploy reads green. The 9100 node-exporter
scrape lost 5.4h to the same class on 2026-08-23; the coredns-host scrape lost the afternoon
of 2026-09-06. Both allows must exist, one per source shape.
"""

from _helpers import ROLES as _ROLES
from _helpers import load_tasks, leaf_tasks

_NODE_TASKS = _ROLES / "setup/k3s/tasks/node.yml"
_UFW = "community.general.ufw"
_PORT_VAR = "k3s_host_dns_metrics_port"


def _metrics_port_allows(tasks: list[dict]) -> list[dict]:
    return [
        t[_UFW]
        for t in leaf_tasks(tasks)
        if _UFW in t
        and _PORT_VAR in str(t[_UFW].get("port", ""))
        and t[_UFW].get("rule") == "allow"
    ]


def metrics_port_sources_missing(tasks: list[dict]) -> list[str]:
    """The source variables no metrics-port allow covers; empty when both shapes are admitted."""
    sources = {a.get("from_ip", "") for a in _metrics_port_allows(tasks)}
    missing = []
    if not any("k3s_pod_cidr" in s for s in sources):
        missing.append("k3s_pod_cidr")
    loops = {
        str(t.get("loop", ""))
        for t in leaf_tasks(tasks)
        if _UFW in t and _PORT_VAR in str(t[_UFW].get("port", ""))
    }
    if not any("k3s_agent_node_ips" in loop for loop in loops):
        missing.append("k3s_agent_node_ips")
    return missing


def _ufw_task(from_ip: str, loop: str | None = None) -> dict:
    task = {
        "name": "allow",
        _UFW: {
            "rule": "allow",
            "from_ip": from_ip,
            "port": "{{ k3s_host_dns_metrics_port }}",
            "proto": "tcp",
        },
    }
    if loop is not None:
        task["loop"] = loop
    return task


def test_the_live_node_tasks_admit_both_source_shapes() -> None:
    missing = metrics_port_sources_missing(load_tasks(_NODE_TASKS))
    assert missing == [], (
        f"no metrics-port allow reads {missing}; a scrape from that source is dropped"
    )


def test_both_allows_are_clean() -> None:
    tasks = [
        _ufw_task("{{ k3s_pod_cidr }}"),
        _ufw_task("{{ item }}", loop="{{ k3s_agent_node_ips }}"),
    ]
    assert metrics_port_sources_missing(tasks) == []


def test_the_pod_cidr_allow_alone_is_flagged() -> None:
    """The shape that shipped: correct while Prometheus sits on this node, wrong when it moves."""
    tasks = [_ufw_task("{{ k3s_pod_cidr }}")]
    assert metrics_port_sources_missing(tasks) == ["k3s_agent_node_ips"]


def test_the_agent_allow_alone_is_flagged() -> None:
    tasks = [_ufw_task("{{ item }}", loop="{{ k3s_agent_node_ips }}")]
    assert metrics_port_sources_missing(tasks) == ["k3s_pod_cidr"]
