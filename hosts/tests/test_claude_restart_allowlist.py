#!/usr/bin/env python3
"""Every workload on the restart broker's allowlist is one a restart cannot hurt or hide.

docs/claude-agent-user.md, "The restart allowlist", states what an entry must satisfy. A failed
restart of a listed workload must cause no outage, must still page, and must open no triage
episode. The first two come from the Deployment the role renders; the third from the poller,
which routes a listed service nowhere, and from `poll_routes` naming no listed service.

Run: uv run pytest hosts/tests/test_claude_restart_allowlist.py
"""

import copy
import json

import pytest
from _k8s_render import pod_spec, rendered_docs
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from lib.guard_jinja_env import template_env

# The roles whose workloads report every other workload's failure. A failed restart there
# removes the thing that would page it.
ALERT_PLANE = frozenset(
    {"observability", "blackbox-exporter", "monitor-bridge", "loki-homelab"}
)
NO_PROFILE = "none"
# Every kind whose pods carry the role as their alerts' `service`.
WORKLOADS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"})


def pins() -> tuple[dict, dict]:
    """The allowlist and the route table claude-triage-pins.j2 exports."""
    env = template_env(CLAUDE_CODE / "templates")
    module = env.get_template("claude-triage-pins.j2").make_module(
        role_context(CLAUDE_CODE)
    )
    allowlist, routes = (
        getattr(module, name) for name in ("restart_allowlist", "poll_routes")
    )
    return allowlist, routes


def max_unavailable(deployment: dict) -> int:
    """How many replicas a rollout may take down at once, as Kubernetes rounds it."""
    spec = deployment["spec"]
    value = (spec.get("strategy", {}).get("rollingUpdate") or {}).get(
        "maxUnavailable", "25%"
    )
    if isinstance(value, int) or not value.endswith("%"):
        return int(value)
    return spec.get("replicas", 1) * int(value.rstrip("%")) // 100


def entry_problems(entry: dict, docs: list[tuple], routes: dict[str, str]) -> list[str]:
    """Why `entry` must not be on the allowlist; empty when it may.

    Args:
        entry: one allowlist value, with `namespace`, `deployment` and `service`.
        docs: `(role, template, doc)` for every rendered manifest.
        routes: the poller's route table.
    """
    service = entry["service"]
    problems = [
        f"poll_routes routes {key} to {profile}"
        for key, profile in routes.items()
        if key.endswith(f":{service}") and profile != NO_PROFILE
    ]
    if service in ALERT_PLANE:
        problems.append(f"{service} is the alert plane")
    workloads = [
        doc for role, _, doc in docs if role == service and doc.get("kind") in WORKLOADS
    ]
    names = sorted(f"{d['kind']}/{d['metadata']['name']}" for d in workloads)
    if names != [f"Deployment/{entry['deployment']}"]:
        return [*problems, f"role {service} renders the workloads {names}"]
    (deployment,) = workloads
    spec, pod = deployment["spec"], pod_spec(deployment)
    if deployment["metadata"].get("namespace") != entry["namespace"]:
        problems.append("the Deployment is in another namespace")
    if deployment["metadata"].get("labels", {}).get("homelab/role") != service:
        problems.append("its alerts carry another service than the entry names")
    if spec.get("replicas", 1) < 1 or spec.get("paused"):
        problems.append("a restart rolls no pod, so it would read as done")
    if spec.get("strategy", {}).get("type", "RollingUpdate") != "RollingUpdate":
        problems.append("its strategy stops the pod before the new one is Ready")
    elif max_unavailable(deployment) != 0:
        problems.append("its rollout may take a serving replica down")
    for container in pod.get("containers", []):
        if "readinessProbe" not in container:
            problems.append(f"container {container['name']} has no readiness probe")
        if any("hostPort" in port for port in container.get("ports", [])):
            problems.append(f"container {container['name']} holds a host port")
    for container in [*pod.get("initContainers", []), *pod.get("containers", [])]:
        if "@sha256:" not in container.get("image", ""):
            problems.append(f"container {container['name']} is not digest-pinned")
    if pod.get("hostNetwork"):
        problems.append("it holds the node's ports, which the surge pod cannot share")
    if any("persistentVolumeClaim" in volume for volume in pod.get("volumes", [])):
        problems.append("it mounts a claim the surge pod cannot share")
    return problems


def test_every_listed_workload_is_clean() -> None:
    allowlist, routes = pins()
    assert "docs" in allowlist
    docs = list(rendered_docs())
    assert {
        name: entry_problems(entry, docs, routes) for name, entry in allowlist.items()
    } == {name: [] for name in allowlist}


ENTRY = {"namespace": "homelab", "deployment": "web", "service": "web"}
CLEAN = {
    "kind": "Deployment",
    "metadata": {
        "name": "web",
        "namespace": "homelab",
        "labels": {"homelab/role": "web"},
    },
    "spec": {
        "replicas": 1,
        "template": {
            "spec": {
                "containers": [
                    {"name": "web", "image": "web@sha256:0", "readinessProbe": {}}
                ]
            }
        },
    },
}


def _doc(change) -> dict:
    doc = copy.deepcopy(CLEAN)
    change(doc)
    return doc


def _container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["spec"].update(strategy={"type": "Recreate"}),
        lambda d: d["spec"].update(strategy={"rollingUpdate": {"maxUnavailable": 1}}),
        lambda d: d["spec"].update(replicas=0),
        lambda d: d["spec"].update(paused=True),
        lambda d: _container(d).pop("readinessProbe"),
        lambda d: _container(d).update(image="web:latest"),
        lambda d: d["spec"]["template"]["spec"].update(
            initContainers=[{"name": "init", "image": "init:latest"}]
        ),
        lambda d: d["spec"]["template"]["spec"].update(hostNetwork=True),
        lambda d: d["metadata"]["labels"].update({"homelab/role": "site"}),
        lambda d: _container(d).update(ports=[{"containerPort": 80, "hostPort": 80}]),
        lambda d: d["spec"]["template"]["spec"].update(
            volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]
        ),
        lambda d: d["metadata"].update(namespace="observability"),
    ],
    ids=[
        "recreate",
        "max-unavailable-1",
        "scaled-to-zero",
        "paused",
        "no-readiness-probe",
        "tag-image",
        "tag-init-image",
        "host-network",
        "other-role-label",
        "host-port",
        "claim",
        "other-namespace",
    ],
)
def test_a_deployment_a_restart_could_take_down_is_flagged(change) -> None:
    assert entry_problems(ENTRY, [("web", "d.yaml.j2", CLEAN)], {}) == []
    assert entry_problems(ENTRY, [("web", "d.yaml.j2", _doc(change))], {})


@pytest.mark.parametrize("kind", ["Deployment", "CronJob"])
def test_a_role_with_a_second_workload_is_flagged(kind) -> None:
    """The service label is the role, so listing one Deployment unroutes its siblings."""
    sibling = _doc(lambda d: d.update(kind=kind, metadata={"name": "web-cache"}))
    docs = [("web", "d.yaml.j2", CLEAN), ("web", "c.yaml.j2", sibling)]
    assert entry_problems(ENTRY, docs, {})


def test_a_route_naming_a_listed_service_is_flagged() -> None:
    docs = [("web", "d.yaml.j2", CLEAN)]
    assert entry_problems(ENTRY, docs, {"K8sContainerCrashLooping:web": "none"}) == []
    assert entry_problems(ENTRY, docs, {"K8sContainerCrashLooping:web": "triage"})


def test_an_alert_plane_workload_is_flagged() -> None:
    entry = ENTRY | {"service": "observability"}
    assert entry_problems(entry, [("observability", "d.yaml.j2", CLEAN)], {})


def test_the_poller_is_told_every_listed_service() -> None:
    allowlist, _ = pins()
    config = dict(
        line.split("=", 1)
        for line in render_component_text(
            "claude-code", "claude-triage-config.env.j2"
        ).splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert json.loads(config["RESTART_SERVICES"]) == sorted(
        entry["service"] for entry in allowlist.values()
    )
