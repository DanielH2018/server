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
_ROLL_ONE = _REPO / "ansible/roles/k8s/pihole/tasks/roll_one.yml"

INSTANCES = {"pihole", "pihole-2"}
CLAIM_BY_INSTANCE = {"pihole": "pihole-etc", "pihole-2": "pihole-etc-2"}


def _pihole_deployments() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Deployment"
    }


def test_two_instances_are_rendered():
    assert set(_pihole_deployments()) == INSTANCES


def test_both_instances_are_selected_by_the_dns_service():
    """The DNS Service fronts both pods because both carry `app: pihole` and it selects only
    on that label. (The web Service additionally pins to `instance: pihole` — see
    test_only_the_web_service_is_pinned_to_one_instance — so it deliberately does NOT select
    pihole-2; this test covers DNS only.)"""
    dns_selector = next(
        doc["spec"]["selector"]
        for role, _tpl, doc in rendered_docs()
        if role == "pihole"
        and doc.get("kind") == "Service"
        and doc["metadata"]["name"] == "pihole-dns"
    )
    for name, dep in _pihole_deployments().items():
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert all(labels.get(k) == v for k, v in dns_selector.items()), (
            f"{name} is not selected by pihole-dns — it would take no DNS traffic"
        )


def test_instances_do_not_share_a_volume():
    """Assert the claim each instance mounts, not just that the two differ — swapping the
    claim names (moving pihole onto pihole-etc-2) would also pass a uniqueness-only check
    and put the live instance on a blank volume."""
    claim_by_instance = {}
    for name, dep in _pihole_deployments().items():
        for vol in dep["spec"]["template"]["spec"].get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claim_by_instance[name] = vol["persistentVolumeClaim"]["claimName"]
    assert claim_by_instance == CLAIM_BY_INSTANCE, (
        f"expected {CLAIM_BY_INSTANCE}, got {claim_by_instance} — a swap would put the live "
        f"instance on a blank volume"
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


def test_pod_template_carries_a_per_instance_label():
    """`kubectl exec deploy/<name>` resolves through spec.selector, which is `app: pihole`
    on both Deployments and can't carry a per-instance value (selector is immutable). The
    pod-only `instance` label is what lets tasks/main.yml and roll_one.yml address a specific
    instance's pod instead of whichever one the shared selector happens to pick."""
    for name, dep in _pihole_deployments().items():
        assert dep["spec"]["template"]["metadata"]["labels"].get("instance") == name, (
            f"{name}'s pod template is missing its own instance label"
        )
        assert "instance" not in dep["spec"]["selector"]["matchLabels"], (
            "spec.selector is immutable in apps/v1 — adding `instance` there would make "
            "kubectl apply fail on the live Deployment"
        )


def _pihole_services() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Service"
    }


def test_only_the_web_service_is_pinned_to_one_instance():
    """Ruling: web UI pins to instance 1 (Pi-hole v6 keeps sessions in FTL memory), DNS
    stays load-balanced across both (each query is stateless and redundancy is the point)."""
    services = _pihole_services()
    assert services["pihole"]["spec"]["selector"].get("instance") == "pihole", (
        "the web Service must pin to instance 1 or admin sessions 401 at random between "
        "two independent FTLs"
    )
    assert "instance" not in services["pihole-dns"]["spec"]["selector"], (
        "the DNS Service must stay selecting both instances — pinning it defeats the "
        "redundancy this plan exists to add"
    )


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def _flatten_tasks(tasks: list[dict]):
    for task in tasks:
        yield task
        if "block" in task:
            yield from _flatten_tasks(task["block"])


def _roll_one_tasks() -> list[dict]:
    return list(_flatten_tasks(yaml.safe_load(_ROLL_ONE.read_text())))


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


def test_roll_one_restarts_then_waits_for_the_same_instance():
    """Deleting the `rollout status` wait from roll_one.yml — the one line that makes the
    restarts sequential rather than concurrent — must fail this test even though every other
    guard in this file still passes."""
    tasks = _roll_one_tasks()

    def _cmd(task: dict) -> str:
        return str(task.get("ansible.builtin.command", {}).get("cmd", ""))

    restart_idx = next(
        (i for i, t in enumerate(tasks) if "rollout restart" in _cmd(t)), None
    )
    status_idx = next(
        (i for i, t in enumerate(tasks) if "rollout status" in _cmd(t)), None
    )
    assert restart_idx is not None, "roll_one.yml is missing the rollout restart"
    assert status_idx is not None, "roll_one.yml is missing the rollout status wait"
    assert restart_idx < status_idx, "the wait must come after the restart"
    for idx in (restart_idx, status_idx):
        assert "pihole_instance" in _cmd(tasks[idx]), (
            "the restart and wait must target pihole_instance, not a hardcoded name"
        )


def test_roll_one_checks_sibling_readiness_before_restarting():
    """Restarting an instance with no ready sibling is a LAN-wide DNS outage (both
    Deployments use Recreate on a single-writer volume)."""
    tasks = _roll_one_tasks()
    ready_check = next(
        (
            t
            for t in tasks
            if "instance=" in str(t.get("ansible.builtin.command", {}).get("cmd", ""))
            and "failed_when" in t
        ),
        None,
    )
    assert ready_check is not None, (
        "roll_one.yml must verify the sibling instance is ready before restarting — "
        "otherwise the first deploy (or a week-stale sibling) is a full DNS outage"
    )


def test_roll_one_skips_an_instance_this_run_just_created():
    assert any(
        "created" in str(t.get("when", ""))
        and "manifests_apply" in str(t.get("when", ""))
        for t in yaml.safe_load(_ROLL_ONE.read_text())
    ), (
        "roll_one.yml must skip the restart+wait for a Deployment this run just created — "
        "restarting it races the initial rollout"
    )
