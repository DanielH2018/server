"""LAN DNS survives a Pi-hole deploy only while three properties hold.

Each of them fails green — the deploy succeeds and DNS goes down anyway:

  * one instance instead of two: the Service has a single backend and its Recreate gap is
    a LAN-wide DNS outage, which is the state this replaced;
  * both restarted at once: the shared manifests role fires restarts back to back and defers
    waiting to the end-of-batch drain, so a reintroduced `manifests_rollout` would take both
    down within a second of each other and the redundancy would be decorative;
  * split across nodes: every VIP is announced from daniel-box only (marked PERMANENT in
    setup/k3s metallb-pool.yaml.j2), and with externalTrafficPolicy: Local a pod on the other
    node receives nothing, so half the capacity would silently serve no traffic.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from _k8s_render import rendered_docs

_REPO = Path(__file__).resolve().parents[2]
_TASKS = _REPO / "ansible/roles/k8s/pihole/tasks/main.yml"

INSTANCES = {"pihole", "pihole-2"}


def _pihole_deployments() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Deployment"
    }


def test_two_instances_are_rendered():
    assert set(_pihole_deployments()) == INSTANCES


def test_both_instances_are_selected_by_the_dns_service():
    """The Services front both pods only because both carry `app: pihole`."""
    selectors = [
        doc["spec"]["selector"]
        for role, _tpl, doc in rendered_docs()
        if role == "pihole"
        and doc.get("kind") == "Service"
        and doc["spec"].get("selector")
    ]
    assert selectors, "no selecting Service found for pihole"
    for name, dep in _pihole_deployments().items():
        labels = dep["spec"]["template"]["metadata"]["labels"]
        for sel in selectors:
            assert all(labels.get(k) == v for k, v in sel.items()), (
                f"{name} is not selected by a pihole Service — it would take no DNS traffic"
            )


def test_instances_do_not_share_a_volume():
    claims = []
    for dep in _pihole_deployments().values():
        for vol in dep["spec"]["template"]["spec"].get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claims.append(vol["persistentVolumeClaim"]["claimName"])
    assert len(claims) == len(set(claims)), (
        f"both instances mount the same RWO claim {claims} — the second pod cannot start"
    )


def test_both_instances_pin_to_the_announcing_node():
    nodes = {
        name: dep["spec"]["template"]["spec"]
        .get("nodeSelector", {})
        .get("kubernetes.io/hostname")
        for name, dep in _pihole_deployments().items()
    }
    assert set(nodes.values()) == {"daniel-box"}, (
        f"every VIP is announced from daniel-box only and the Service is "
        f"externalTrafficPolicy: Local, so a pod elsewhere receives nothing. Got {nodes}"
    )


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def test_the_shared_role_does_not_restart_pihole():
    """`manifests_rollout: ''` is what stops both instances restarting together."""
    for task in _tasks():
        if task.get("ansible.builtin.include_role", {}).get("name") == "k8s/manifests":
            vars_ = task.get("vars", {})
            assert vars_.get("manifests_rollout") == "", (
                "pihole must set manifests_rollout: '' — otherwise the shared role restarts "
                "both instances back to back and defers waiting to the end-of-batch drain"
            )
            assert not vars_.get("manifests_extra_rollouts"), (
                "extra rollouts restart in a batch too; use the sequenced roll_one.yml instead"
            )
            return
    raise AssertionError("pihole no longer includes k8s/manifests")


def test_the_rollout_is_sequenced_per_instance():
    included = [
        task
        for task in _tasks()
        if str(task.get("ansible.builtin.include_tasks", "")).endswith("roll_one.yml")
    ]
    assert included, "no per-instance roll_one.yml include — restarts are not sequenced"
    looped = included[0].get("loop")
    assert set(looped) == INSTANCES, (
        f"roll_one.yml must cover both instances, got {looped}"
    )
