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
import re

import pytest
import yaml
from _k8s_render import pod_spec, rendered_docs
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from lib import yaml_fast
from lib.guard_jinja_env import template_env
from validate.alert_conventions import RULES_CONFIGMAP, _alerts, expression_jobs

# The alert plane no rendered file states. Every pod resolves names through coredns,
# Alertmanager's receivers included, so a failed coredns restart silences every page.
UNSTATED_ALERT_PLANE = frozenset({"coredns"})
# A host Alertmanager notifies: a URL's host, or the SMTP smarthost's.
RECEIVER_HOST = re.compile(r"(?:https?://|smarthost:\s*)([\w.-]+)")
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


def _configmap_values(docs: list[tuple]):
    """`(role, ConfigMap name, key, parsed value)` for each data value that parses as YAML."""
    for role, _, doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for key, text in (doc.get("data") or {}).items():
            try:
                value = yaml_fast.safe_load(text)
            except yaml.YAMLError:
                continue
            yield role, doc["metadata"]["name"], key, value


def _selected_jobs(expr: str) -> set[str]:
    return {
        job
        for matchers in expression_jobs(expr)
        for op, value in matchers
        if op in ("=", "=~")
        for job in value.split("|")
    }


def _receiver_roles(docs: list[tuple]) -> set[str]:
    """The roles rendering a Service that Alertmanager's config Secret names as a host."""
    services = {}
    for role, _, doc in docs:
        if doc.get("kind") == "Service":
            name, namespace = doc["metadata"]["name"], doc["metadata"].get("namespace")
            qualified = f"{name}.{namespace}"
            for host in (
                qualified,
                f"{qualified}.svc",
                f"{qualified}.svc.cluster.local",
            ):
                services[(None, host)] = role
            services[(namespace, name)] = role
    roles = set()
    for _, _, doc in docs:
        values = doc.get("stringData") or {}
        if doc.get("kind") != "Secret" or "alertmanager.yml" not in values:
            continue
        namespace = doc["metadata"].get("namespace")
        for text in values.values():
            for host in RECEIVER_HOST.findall(text):
                role = services.get((None, host)) or services.get((namespace, host))
                roles |= {role} if role else set()
    return roles


def alert_plane(docs: list[tuple]) -> frozenset[str]:
    """The roles whose failure hides another service's page.

    A role is in it when its rendered config sends alerts to Alertmanager: Prometheus's
    `alerting.alertmanagers` or the Loki ruler's `alertmanager_url`. It is in it when it
    renders a Service that Alertmanager's config Secret names as a receiver's host. It is in it too when
    it owns a scrape job, by the literal `service` of that job's `ScrapeTargetDown` rule in
    `scrape-targets.yaml`, and a rule whose `service` is another, or comes from the series,
    reads that job. `UNSTATED_ALERT_PLANE` adds what no file states.

    Args:
        docs: `(role, template, doc)` for every rendered manifest.
    """
    plane = set(UNSTATED_ALERT_PLANE) | _receiver_roles(docs)
    rule_files = {}
    for role, name, key, value in _configmap_values(docs):
        if not isinstance(value, dict):
            continue
        if (value.get("alerting") or {}).get("alertmanagers") or (
            value.get("ruler") or {}
        ).get("alertmanager_url"):
            plane.add(role)
        if name == RULES_CONFIGMAP:
            rule_files[key] = value
    owners = {
        job: rule["labels"]["service"]
        for _, rule in _alerts(rule_files.get("scrape-targets.yaml"))
        if rule["alert"] == "ScrapeTargetDown"
        for job in _selected_jobs(str(rule["expr"]))
    }
    for rule_file in rule_files.values():
        for _, rule in _alerts(rule_file):
            service = (rule.get("labels") or {}).get("service")
            plane |= {
                owners[job]
                for job in _selected_jobs(str(rule["expr"]))
                if job in owners and owners[job] != service
            }
    return frozenset(plane)


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
    if service in alert_plane(docs):
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


def _configmap(name: str, data: dict) -> dict:
    return {
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "data": {key: yaml.safe_dump(value) for key, value in data.items()},
    }


LOKI = {"ruler": {"alertmanager_url": "http://alertmanager:9093"}}
SCRAPE_TARGETS = {
    "groups": [
        {
            "name": "scrape-targets",
            "rules": [
                {
                    "alert": "ScrapeTargetDown",
                    "expr": 'up{job=~"probe|probe-extra"} == 0',
                    "labels": {"service": "prober"},
                }
            ],
        }
    ]
}


def _rules(service: str | None) -> dict:
    labels = {"service": service} if service else {}
    rule = {"alert": "ProbeFailed", "expr": 'probe_success{job="probe-extra"} == 0'}
    return {"groups": [{"name": "probes", "rules": [rule | {"labels": labels}]}]}


def test_an_alert_plane_workload_is_flagged() -> None:
    sender = ("web", "c.yaml.j2", _configmap("web-config", {"loki.yaml": LOKI}))
    assert entry_problems(ENTRY, [("web", "d.yaml.j2", CLEAN)], {}) == []
    assert entry_problems(ENTRY, [("web", "d.yaml.j2", CLEAN), sender], {})


@pytest.mark.parametrize(
    "config",
    [LOKI, {"alerting": {"alertmanagers": [{"static_configs": []}]}}],
    ids=["loki-ruler", "prometheus"],
)
def test_a_role_that_sends_to_alertmanager_is_the_alert_plane(config) -> None:
    def plane(value: dict) -> frozenset[str]:
        docs = [("logs", "c.yaml.j2", _configmap("logs", {"config.yaml": value}))]
        return alert_plane(docs) - UNSTATED_ALERT_PLANE

    assert plane({"server": {}}) == frozenset()
    assert plane(config) == {"logs"}


@pytest.mark.parametrize(
    "url",
    [
        "http://ntfy:80/alerts",
        "http://ntfy.alerts.svc/x",
        "http://ntfy.alerts.svc.cluster.local/x",
    ],
    ids=["bare", "svc", "fqdn"],
)
def test_the_role_behind_an_alertmanager_receiver_is_the_alert_plane(url) -> None:
    def plane(receiver_url: str) -> frozenset[str]:
        service = {
            "kind": "Service",
            "metadata": {"name": "ntfy", "namespace": "alerts"},
        }
        secret = {
            "kind": "Secret",
            "metadata": {"name": "am", "namespace": "alerts"},
            "stringData": {"alertmanager.yml": "receivers: []", "url": receiver_url},
        }
        docs = [("ntfy", "s.yaml.j2", service), ("o11y", "a.yaml.j2", secret)]
        return alert_plane(docs) - UNSTATED_ALERT_PLANE

    assert plane("https://discord.com/api/webhooks/1") == frozenset()
    assert plane(url) == {"ntfy"}


@pytest.mark.parametrize("service", [None, "web"], ids=["from-series", "other"])
def test_the_owner_of_a_job_another_services_rule_reads_is_the_alert_plane(
    service,
) -> None:
    def plane(rules: dict) -> frozenset[str]:
        data = {"scrape-targets.yaml": SCRAPE_TARGETS, "probes.yaml": rules}
        docs = [("o11y", "r.yaml.j2", _configmap(RULES_CONFIGMAP, data))]
        return alert_plane(docs) - UNSTATED_ALERT_PLANE

    assert plane(_rules("prober")) == frozenset()
    assert plane(_rules(service)) == {"prober"}


def test_the_alert_plane_holds_every_role_that_reports_on_others() -> None:
    assert alert_plane(list(rendered_docs())) >= {
        "observability",
        "blackbox-exporter",
        "monitor-bridge",
        "loki-homelab",
        "coredns",
    }


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
