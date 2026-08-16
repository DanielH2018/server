#!/usr/bin/env python3
"""Render a self-contained HTML map of the homelab infrastructure.

Two layers are merged into one page:

* **Declared state** — ``containers_list`` from ``ansible/inventory/host_vars/``,
  the source of truth for what is *supposed* to run where. This is what changes
  when you edit the repo.
* **Live state** — ``kubectl`` against the k3s cluster and ``docker ps`` on
  daniel-pi, overlaid onto the declared skeleton so drift is visible.

The page opens with an architecture diagram: the request path (DNS → ingress VIP
→ Traefik → Authelia → workloads), the cluster's two nodes, the Longhorn backup
chain, and the Pi's LAN-only plane. Its shape is a fixed skeleton — those edges
live in role templates, not in ``containers_list`` — but every number, address,
name and status colour on it is read from the inventory and the live cluster, so
it tracks changes without being hand-edited.

The output is a single ``.html`` file with no external assets, safe to open over
``file://``. Re-running overwrites it in place, so a cron entry keeps an open tab
current (the page carries its own ``<meta http-equiv="refresh">``).

An unreachable host degrades to declared-only rather than failing the render —
this runs unattended, and a partial map beats no map.

Usage::

    uv run python scripts/gen_infra_map.py                     # default output path
    uv run python scripts/gen_infra_map.py -o /tmp/map.html
    uv run python scripts/gen_infra_map.py --no-live           # declared state only
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = Path.home() / ".claude" / "artifacts" / "homelab-infra-map.html"

HOSTS = ("daniel-box", "daniel-server", "daniel-pi")

# What each host actually is. Stated rather than inferred from the platform keys
# in its ``containers_list``: daniel-server's Docker was uninstalled on
# 2026-08-14 and its list emptied, so inference fell through to "docker" and
# every run ssh-ed it for a binary that is gone — the host rendered as an
# unreachable Docker box when it is in fact a healthy k3s agent.
HOST_PLANE = {
    "daniel-box": "k8s",
    "daniel-server": "k8s",
    "daniel-pi": "docker",
}

# The sub-role within the plane, for the host panel and the diagram's node boxes.
HOST_ROLE = {
    "daniel-box": "k3s server · control plane",
    "daniel-server": "k3s agent",
    "daniel-pi": "Docker · LAN-only",
}

# How long the rendered page waits before reloading itself, in seconds. Matches
# the refresh cron's 15-minute period (initial_setup's `infra-map` task) — a
# shorter reload would imply the data is fresher than it is.
PAGE_REFRESH_SECONDS = 900

# Live-collection timeouts. Short on purpose: an unattended run must not hang.
LOCAL_TIMEOUT = 20
SSH_TIMEOUT = 25

_CONTAINER_NAME = re.compile(
    r"^\s*container_name:\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE
)

_MANIFEST_KIND = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)

# `claude-otel` is one declared entry that expands to the whole observability
# namespace (collector + loki + prometheus + tempo + grafana). Nothing else in
# the inventory owns a namespace, so an explicit entry beats deriving it by
# rendering the role's Jinja namespace manifest.
NAMESPACE_OWNERS = {"claude-otel": "k8s_observability_namespace"}

_JINJA_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# --------------------------------------------------------------------------
# Inventory (declared state)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleIndex:
    """What the repo says each role actually creates.

    ``container_owners`` maps a container name to the role whose compose file
    declares it. ``batch_roles`` are roles that run something one-shot and leave
    nothing behind — a CronJob role fires and exits, and the k8s image-build
    roles ship no manifests — so "not running" is correct for them, not a fault.
    """

    container_owners: dict[str, str]
    batch_roles: frozenset[str]


def load_roles(repo_root: Path = REPO_ROOT) -> RoleIndex:
    """Build the role index by reading the role trees, not by guessing names."""
    owners: dict[str, str] = {}
    batch: set[str] = set()

    docker_roles = repo_root / "ansible" / "roles" / "containers"
    for role in sorted(p for p in docker_roles.glob("*") if p.is_dir()):
        compose = role / "templates" / "docker-compose.yml.j2"
        if not compose.is_file():
            continue
        names = _CONTAINER_NAME.findall(compose.read_text())
        if not names:
            batch.add(role.name)
        for name in names:
            owners[name] = role.name

    # A k8s role with no templates directory builds images or seeds volumes; it
    # has no Deployment to find.
    k8s_roles = repo_root / "ansible" / "roles" / "k8s"
    for role in sorted(p for p in k8s_roles.glob("*") if p.is_dir()):
        templates = role / "templates"
        if not templates.is_dir():
            batch.add(role.name)
            continue
        # A role whose only workload is a CronJob leaves nothing running between
        # firings. Derived here rather than from the Docker compose that used to
        # declare no container name — that plumbing was deleted with the migration.
        kinds = {
            kind
            for tpl in templates.glob("*.yaml.j2")
            for kind in _MANIFEST_KIND.findall(tpl.read_text())
        }
        if "CronJob" in kinds and "Deployment" not in kinds:
            batch.add(role.name)

    return RoleIndex(container_owners=owners, batch_roles=frozenset(batch))


def resolve_vars(value: Any, variables: dict[str, Any], _depth: int = 0) -> Any:
    """Substitute simple ``{{ name }}`` references from *variables*.

    Only bare-variable interpolation is supported — that is all the inventory
    uses for the keys this map reads (``hostname``, namespace names). Filters,
    expressions, and unknown names are left untouched so they show up verbatim
    in the output rather than being silently blanked.
    """
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        return str(variables[name])

    resolved = _JINJA_VAR.sub(replace, value)
    # Values can reference other templated values (k8s_registry_pull_host).
    if resolved != value and _depth < 5 and _JINJA_VAR.search(resolved):
        return resolve_vars(resolved, variables, _depth + 1)
    return resolved


def load_inventory(
    repo_root: Path = REPO_ROOT,
) -> tuple[dict[str, Any], dict[str, dict]]:
    """Return ``(global_vars, {host: host_vars})`` from the Ansible inventory."""
    inventory = repo_root / "ansible" / "inventory"
    global_vars = (
        yaml.safe_load((inventory / "group_vars" / "all.yml").read_text()) or {}
    )
    host_vars: dict[str, dict] = {}
    for host in HOSTS:
        path = inventory / "host_vars" / f"{host}.yml"
        host_vars[host] = (
            (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        )
    return global_vars, host_vars


def declared_services(host: str, host_vars: dict, global_vars: dict) -> list[dict]:
    """Flatten a host's ``containers_list`` into normalized service records."""
    variables = {**global_vars, **host_vars}
    services = []
    for entry in host_vars.get("containers_list") or []:
        name = entry.get("name")
        if not name:
            continue
        platform = entry.get("platform", "docker")
        hostname = resolve_vars(entry.get("hostname", name), variables)
        namespace = None
        if platform == "k8s":
            ns_var = NAMESPACE_OWNERS.get(name)
            namespace = (
                variables.get(ns_var) if ns_var else variables.get("k8s_namespace")
            )
        services.append(
            {
                "name": name,
                "host": host,
                "platform": platform,
                "hostname": hostname if entry.get("port") else None,
                "port": entry.get("port"),
                "authelia": bool(entry.get("use_authelia")),
                "networks": list(entry.get("networks") or []),
                "namespace": namespace,
                "declared": True,
                "status": "unknown",
                "detail": "",
                "image": "",
                "replicas": None,
            }
        )
    return services


# --------------------------------------------------------------------------
# Live state
# --------------------------------------------------------------------------


# Directories searched for the collector binaries, on top of whatever PATH the
# caller happens to have. cron runs with PATH=/usr/bin:/bin, which omits
# /usr/local/bin — where kubectl lives as a symlink to k3s. Resolving tools here
# rather than trusting PATH is what stops an impoverished environment from
# silently blinding half the map; see MissingToolError for the other half.
TOOL_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/snap/bin")


class MissingToolError(Exception):
    """A collector binary is absent — a broken setup, not a host being down.

    Kept distinct from an unreachable host on purpose. A host that is down is an
    observation worth rendering; a missing binary means this run could never
    have seen anything, and a page that quietly reports declared-only in that
    case looks identical to a healthy one. This escalates to a non-zero exit so
    cron surfaces it instead of overwriting the page with a half-blind copy.
    """


def find_tool(name: str) -> str | None:
    """Resolve a binary by absolute path, searching beyond the inherited PATH."""
    found = shutil.which(name)
    if found:
        return found
    search = os.pathsep.join(TOOL_DIRS)
    return shutil.which(name, path=search)


# k3s ships kubectl as a symlink to itself and defaults it at this file, which is
# root-owned 0640. An interactive shell exports KUBECONFIG to the user copy, so
# `kubectl get` works by hand and fails under cron — the same ambient-environment
# trap as PATH, one variable over.
K3S_KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
USER_KUBECONFIG = Path.home() / ".kube" / "config"


def find_kubeconfig() -> Path | None:
    """Pick a kubeconfig this process can actually read.

    Returned explicitly and passed as ``--kubeconfig`` rather than left to
    kubectl's own lookup, so the answer does not change with the caller's
    environment. Readability is checked here, not assumed: the k3s default is
    root-only, and discovering that at exec time yields a warning on stderr and
    an empty result, which reads exactly like a cluster with no deployments.
    """
    candidates = []
    env_path = os.environ.get("KUBECONFIG", "").strip()
    if env_path:
        # KUBECONFIG is a path LIST; kubectl merges the entries left to right.
        candidates.extend(Path(p) for p in env_path.split(os.pathsep) if p)
    candidates.extend((USER_KUBECONFIG, K3S_KUBECONFIG))
    for candidate in candidates:
        if os.access(candidate, os.R_OK):
            return candidate
    return None


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """Run *cmd*, returning ``(ok, stdout-or-error)``. Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:400]
    return True, proc.stdout


DOCKER_PS_FORMAT = "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"


def parse_docker_ps(output: str) -> dict[str, dict]:
    """Parse tab-separated ``docker ps -a`` output keyed by container name."""
    containers: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or not parts[0].strip():
            continue
        name, state, status, image = (p.strip() for p in parts[:4])
        containers[name] = {
            "state": state,
            "status": status,
            "image": image,
            "healthy": "(healthy)" in status,
            "unhealthy": "(unhealthy)" in status,
        }
    return containers


def collect_docker(host: str, local_hostname: str) -> tuple[bool, dict[str, dict], str]:
    """Collect Docker state for *host*, locally or over a single ssh call."""
    if host == local_hostname:
        docker = find_tool("docker")
        if docker is None:
            raise MissingToolError("docker not found on this host")
        cmd = [docker, "ps", "-a", "--format", DOCKER_PS_FORMAT]
        timeout = LOCAL_TIMEOUT
    else:
        ssh = find_tool("ssh")
        if ssh is None:
            raise MissingToolError("ssh not found on this host")
        # One ssh invocation, not one per service: `ufw limit ssh` rejects
        # 6+ connections per 30s and would ban an over-eager generator.
        remote = "docker ps -a --format '%s'" % DOCKER_PS_FORMAT
        cmd = [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={SSH_TIMEOUT}",
            host,
            remote,
        ]
        timeout = SSH_TIMEOUT + 10
    ok, out = _run(cmd, timeout)
    if not ok:
        return False, {}, out
    return True, parse_docker_ps(out), ""


def parse_kubectl_deployments(payload: str) -> dict[tuple[str, str], dict]:
    """Parse ``kubectl get deployments -A -o json`` into ``{(ns, name): info}``."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    workloads: dict[tuple[str, str], dict] = {}
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        name, namespace = meta.get("name"), meta.get("namespace")
        if not name or not namespace:
            continue
        status = item.get("status", {})
        spec = item.get("spec", {})
        desired = spec.get("replicas", 0)
        ready = status.get("readyReplicas", 0) or 0
        containers = (
            item.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        workloads[(namespace, name)] = {
            "ready": ready,
            "desired": desired,
            "image": containers[0].get("image", "") if containers else "",
        }
    return workloads


def collect_k8s(host: str, local_hostname: str) -> tuple[bool, dict, str]:
    """Collect Deployment state from the cluster (local kubectl only)."""
    if host != local_hostname:
        return False, {}, f"kubectl only queried locally; run this on {host}"
    kubectl = find_tool("kubectl")
    if kubectl is None:
        raise MissingToolError("kubectl not found on this host")
    kubeconfig = find_kubeconfig()
    if kubeconfig is None:
        raise MissingToolError(
            f"no readable kubeconfig (tried $KUBECONFIG, {USER_KUBECONFIG}, {K3S_KUBECONFIG})"
        )
    ok, out = _run(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "deployments",
            "-A",
            "-o",
            "json",
        ],
        LOCAL_TIMEOUT,
    )
    if not ok:
        return False, {}, out
    return True, parse_kubectl_deployments(out), ""


# Pod placement comes from a column projection rather than `-o json`: the whole
# pod list is megabytes, and the only fields the diagram needs are these four.
POD_COLUMNS = (
    "NAMESPACE:.metadata.namespace,NAME:.metadata.name,"
    "NODE:.spec.nodeName,PHASE:.status.phase"
)


def parse_kubectl_nodes(payload: str) -> dict[str, dict]:
    """Parse ``kubectl get nodes -o json`` into ``{name: info}``."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    nodes: dict[str, dict] = {}
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        conditions = {
            c.get("type"): c.get("status")
            for c in item.get("status", {}).get("conditions", [])
        }
        roles = sorted(
            label.split("/", 1)[1]
            for label in meta.get("labels", {})
            if label.startswith("node-role.kubernetes.io/")
        )
        addresses = {
            a.get("type"): a.get("address")
            for a in item.get("status", {}).get("addresses", [])
        }
        node_info = item.get("status", {}).get("nodeInfo", {})
        nodes[name] = {
            "ready": conditions.get("Ready") == "True",
            "roles": roles,
            "ip": addresses.get("InternalIP", ""),
            "version": node_info.get("kubeletVersion", ""),
            "schedulable": not item.get("spec", {}).get("unschedulable", False),
        }
    return nodes


def parse_pod_placement(output: str) -> list[dict]:
    """Parse the ``POD_COLUMNS`` projection into pod records."""
    pods = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        namespace, name, node, phase = parts
        pods.append(
            {
                "namespace": namespace,
                "name": name,
                # kubectl prints <none> for a pod that has not been scheduled.
                "node": "" if node == "<none>" else node,
                "phase": phase,
            }
        )
    return pods


def parse_backup_targets(payload: str) -> list[dict]:
    """Parse ``kubectl get backuptargets.longhorn.io -A -o json``.

    An empty ``backupTargetURL`` is how this repo disarms a target, so a blank
    URL is "disarmed", not "misconfigured" — the two look identical in the CR
    and only the arming convention tells them apart.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    targets = []
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        url = item.get("spec", {}).get("backupTargetURL", "") or ""
        status = item.get("status", {})
        targets.append(
            {
                "name": name,
                "url": url,
                "armed": bool(url),
                "available": bool(status.get("available")),
            }
        )
    return sorted(targets, key=lambda t: t["name"])


def collect_cluster(local_hostname: str, longhorn_namespace: str) -> dict:
    """Collect the cluster-wide state the diagram draws from.

    Deployments are collected by :func:`collect_k8s`; everything here is extra
    context that has no declared counterpart in ``containers_list`` — node
    readiness, which node each pod landed on, and the Longhorn backup chain.
    Each query degrades on its own: a missing Longhorn CRD costs the storage
    panel, not the page.
    """
    empty = {
        "ok": False,
        "error": f"kubectl only queried locally; run this on {local_hostname}",
        "nodes": {},
        "pods": [],
        "volumes": None,
        "backup_targets": [],
    }
    if HOST_PLANE.get(local_hostname) != "k8s":
        return empty

    kubectl = find_tool("kubectl")
    if kubectl is None:
        raise MissingToolError("kubectl not found on this host")
    kubeconfig = find_kubeconfig()
    if kubeconfig is None:
        raise MissingToolError(
            f"no readable kubeconfig (tried $KUBECONFIG, {USER_KUBECONFIG}, {K3S_KUBECONFIG})"
        )
    base = [kubectl, "--kubeconfig", str(kubeconfig)]

    ok, out = _run(base + ["get", "nodes", "-o", "json"], LOCAL_TIMEOUT)
    if not ok:
        return {**empty, "error": out}
    nodes = parse_kubectl_nodes(out)

    ok, out = _run(
        base
        + ["get", "pods", "-A", "--no-headers", "-o", f"custom-columns={POD_COLUMNS}"],
        LOCAL_TIMEOUT,
    )
    pods = parse_pod_placement(out) if ok else []

    ok, out = _run(
        base
        + [
            "get",
            "volumes.longhorn.io",
            "-n",
            longhorn_namespace,
            "--no-headers",
            "-o",
            "custom-columns=NAME:.metadata.name",
        ],
        LOCAL_TIMEOUT,
    )
    volumes = len([line for line in out.splitlines() if line.strip()]) if ok else None

    ok, out = _run(
        base + ["get", "backuptargets.longhorn.io", "-A", "-o", "json"], LOCAL_TIMEOUT
    )
    targets = parse_backup_targets(out) if ok else []

    return {
        "ok": True,
        "error": "",
        "nodes": nodes,
        "pods": pods,
        "volumes": volumes,
        "backup_targets": targets,
    }


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def match_k8s_workloads(
    service: dict, workloads: dict[tuple[str, str], dict]
) -> list[dict]:
    """Find the Deployments backing a declared k8s service.

    A namespace owner (``claude-otel``) claims every Deployment in its
    namespace; everything else matches its own name plus ``<name>-*`` helper
    Deployments in the app namespace.
    """
    name, namespace = service["name"], service.get("namespace")
    matched = []
    if name in NAMESPACE_OWNERS:
        for (ns, wl_name), info in workloads.items():
            if ns == namespace:
                matched.append({"name": wl_name, "namespace": ns, **info})
    else:
        for (ns, wl_name), info in workloads.items():
            if ns != namespace:
                continue
            if wl_name == name or wl_name.startswith(f"{name}-"):
                matched.append({"name": wl_name, "namespace": ns, **info})
    return sorted(matched, key=lambda w: w["name"])


def reconcile_docker(
    service: dict, containers: dict[str, dict], roles: RoleIndex
) -> dict:
    """Overlay live Docker state onto one declared service."""
    live = containers.get(service["name"])
    if live is None:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "one-shot job — leaves no container behind",
            }
        return {**service, "status": "missing", "detail": "no container found"}
    if live["state"] != "running":
        return {
            **service,
            "status": "down",
            "detail": live["status"],
            "image": live["image"],
        }
    status = "degraded" if live["unhealthy"] else "healthy"
    return {
        **service,
        "status": status,
        "detail": live["status"],
        "image": live["image"],
    }


def reconcile_k8s(
    service: dict, workloads: dict[tuple[str, str], dict], roles: RoleIndex
) -> dict:
    """Overlay live Deployment state onto one declared k8s service."""
    matched = match_k8s_workloads(service, workloads)
    if not matched:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "build/seed role — no long-running workload",
            }
        return {**service, "status": "missing", "detail": "no deployment found"}
    ready = sum(w["ready"] for w in matched)
    desired = sum(w["desired"] for w in matched)
    detail = f"{ready}/{desired} replicas ready across {len(matched)} deployment"
    detail += "s" if len(matched) != 1 else ""
    if ready == 0:
        status = "down"
    elif ready < desired:
        status = "degraded"
    else:
        status = "healthy"
    return {
        **service,
        "status": status,
        "detail": detail,
        "image": matched[0]["image"],
        "replicas": (ready, desired),
        "workloads": matched,
    }


def place_on_nodes(service: dict, pods: list[dict]) -> dict:
    """Record which cluster nodes a k8s service's pods actually landed on.

    Placement is not in ``containers_list`` and not on the Deployment either —
    ``.spec.nodeName`` is a pod field — so it can only come from the live pod
    list. It matters here because several failures in this cluster have been
    placement-dependent rather than workload-dependent.
    """
    nodes = set()
    for workload in service.get("workloads") or []:
        prefix = f"{workload['name']}-"
        for pod in pods:
            if (
                pod["namespace"] == workload["namespace"]
                and pod["name"].startswith(prefix)
                and pod["node"]
            ):
                nodes.add(pod["node"])
    return {**service, "nodes": sorted(nodes)}


def find_extra_containers(
    containers: dict[str, dict], declared_names: set[str], roles: RoleIndex
) -> list[dict]:
    """Classify live containers that have no ``containers_list`` entry.

    Most are companions: a role's compose file defines several containers but
    only the main one earns an inventory entry (the prometheus role also brings
    node-exporter and cadvisor). Those are expected. Anything the repo does not
    account for at all is real drift, and only that gets flagged as undeclared.
    """
    extras = []
    for name, live in sorted(containers.items()):
        if name in declared_names:
            continue
        owner = roles.container_owners.get(name)
        if owner and owner in declared_names:
            status, detail = (
                "companion",
                f"owned by the {owner} role · {live['status']}",
            )
        elif live["state"] == "running":
            status, detail = "undeclared", live["status"]
        else:
            status, detail = "down", live["status"]
        extras.append(
            {
                "name": name,
                "platform": "docker",
                "hostname": None,
                "port": None,
                "authelia": False,
                "networks": [],
                "namespace": None,
                "declared": False,
                "owner": owner,
                "status": status,
                "detail": detail,
                "image": live["image"],
                "replicas": None,
            }
        )
    return extras


def services_on_host(
    host: str, declared_here: list[dict], k8s_services: list[dict]
) -> list[dict]:
    """What a k3s host is actually running, rather than what declares it.

    Every k8s entry in the inventory is declared under daniel-box, so listing a
    host by its own ``containers_list`` renders daniel-server empty while it
    runs half the fleet. Placement is the honest answer to "what is on this
    box", so a service is shown wherever its pods landed — on both hosts when
    it is spread across both. A service with no pods anywhere (a one-shot job,
    or something genuinely missing) stays with the host that declares it, so
    nothing drops off the page.
    """
    placed = [s for s in k8s_services if host in (s.get("nodes") or [])]
    unplaced = [s for s in declared_here if not s.get("nodes")]
    return sorted(placed + unplaced, key=lambda s: s["name"])


def build_model(
    global_vars: dict,
    host_vars: dict[str, dict],
    live: dict[str, dict],
    generated_at: str,
    roles: RoleIndex,
    cluster: dict | None = None,
) -> dict:
    """Merge declared and live state into the structure the renderer consumes.

    *live* maps a host name to ``{"ok": bool, "error": str, "data": ...}``.
    *cluster* is the extra cluster-wide state from :func:`collect_cluster`, or
    None when it was not collected. Pure — every side effect happens before this
    is called, which is what makes the whole reconciliation layer testable.
    """
    cluster = cluster or {
        "ok": False,
        "error": "not collected",
        "nodes": {},
        "pods": [],
        "volumes": None,
        "backup_targets": [],
    }
    pods_by_node: dict[str, int] = {}
    for pod in cluster["pods"]:
        if pod["node"]:
            pods_by_node[pod["node"]] = pods_by_node.get(pod["node"], 0) + 1
    hosts = []
    per_host_services: dict[str, list[dict]] = {}

    for host in HOSTS:
        hv = host_vars.get(host, {})
        declared = declared_services(host, hv, global_vars)
        info = live.get(host, {"ok": False, "error": "not collected", "data": {}})
        platform = HOST_PLANE.get(host) or (
            "k8s" if any(s["platform"] == "k8s" for s in declared) else "docker"
        )

        if info["ok"]:
            declared_names = {s["name"] for s in declared}
            if platform == "k8s":
                services = [
                    place_on_nodes(
                        reconcile_k8s(s, info["data"], roles), cluster["pods"]
                    )
                    for s in declared
                ]
            else:
                services = [reconcile_docker(s, info["data"], roles) for s in declared]
                services += find_extra_containers(info["data"], declared_names, roles)
        else:
            services = declared

        services.sort(key=lambda s: (not s["declared"], s["name"]))
        per_host_services[host] = services

        node = cluster["nodes"].get(host) if platform == "k8s" else None
        hosts.append(
            {
                "name": host,
                "ip": hv.get("server_ip", ""),
                "platform": platform,
                "role": HOST_ROLE.get(host, ""),
                "node": ({**node, "pods": pods_by_node.get(host, 0)} if node else None),
                "reachable": info["ok"],
                "error": info.get("error", ""),
                "declared_count": len(declared),
            }
        )

    # `per_host_services` stays declaration-based — it is the canonical list the
    # table, the totals and the grouping count exactly once. The host panels
    # answer a different question ("what is on this box"), and for the k3s hosts
    # that is placement, not declaration.
    k8s_services = [
        s
        for host, services in per_host_services.items()
        if HOST_PLANE.get(host) == "k8s"
        for s in services
    ]
    for host in hosts:
        declared_here = per_host_services[host["name"]]
        shown = (
            services_on_host(host["name"], declared_here, k8s_services)
            if host["platform"] == "k8s"
            else declared_here
        )
        counts: dict[str, int] = {}
        for service in shown:
            counts[service["status"]] = counts.get(service["status"], 0) + 1
        host["services"] = shown
        host["counts"] = counts
        host["routed_count"] = sum(1 for s in shown if s["hostname"])
        host["authelia_count"] = sum(1 for s in shown if s["authelia"])

    all_services = [s for services in per_host_services.values() for s in services]
    totals = {
        "services": len(all_services),
        "healthy": sum(1 for s in all_services if s["status"] == "healthy"),
        "degraded": sum(1 for s in all_services if s["status"] == "degraded"),
        "down": sum(1 for s in all_services if s["status"] in ("down", "missing")),
        "job": sum(1 for s in all_services if s["status"] == "job"),
        "companion": sum(1 for s in all_services if s["status"] == "companion"),
        "undeclared": sum(1 for s in all_services if s["status"] == "undeclared"),
        "unknown": sum(1 for s in all_services if s["status"] == "unknown"),
    }
    return {
        "generated_at": generated_at,
        "hosts": hosts,
        "services": all_services,
        "totals": totals,
        "domain": global_vars.get("domain", ""),
        "hostname_suffix": global_vars.get("k8s_hostname_suffix", ""),
        "cluster": {
            "ok": cluster["ok"],
            "error": cluster["error"],
            "nodes": [
                {"name": name, "pods": pods_by_node.get(name, 0), **info}
                for name, info in sorted(cluster["nodes"].items())
            ],
            "pod_count": len(cluster["pods"]),
            "volumes": cluster["volumes"],
            "backup_targets": cluster["backup_targets"],
        },
        # Every address the diagram labels an edge with, read from the inventory
        # rather than written into the drawing. Renaming a VIP in group_vars
        # moves the label; it does not leave a stale one behind.
        "endpoints": {
            "ingress_vip": global_vars.get("k3s_metallb_ingress_vip", ""),
            "dns_vip": global_vars.get("dns_k8s_vip", ""),
            "mqtt_vip": global_vars.get("mqtt_k8s_vip", ""),
            "jellyfin_vip": global_vars.get("jellyfin_k8s_lan_ip", ""),
            "public_routes": bool(global_vars.get("k8s_public_route")),
            "longhorn_namespace": global_vars.get(
                "k8s_longhorn_namespace", "longhorn-system"
            ),
        },
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

# Catppuccin Mocha, matching the terminal these pages are generated from.
STYLE = """
:root {
  --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
  --surface0: #313244; --surface1: #45475a; --surface2: #585b70;
  --text: #cdd6f4; --subtext0: #a6adc8; --overlay0: #6c7086;
  --blue: #89b4fa; --green: #a6e3a1; --yellow: #f9e2af; --red: #f38ba8;
  --mauve: #cba6f7; --peach: #fab387; --teal: #94e2d5; --lavender: #b4befe;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.5rem 4rem;
  background: var(--base); color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6; font-size: 15px;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.85rem; font-weight: 650; letter-spacing: -0.02em; margin: 0 0 .35rem; }
h2 {
  font-size: 1.05rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--subtext0); margin: 3rem 0 1rem; padding-bottom: .5rem;
  border-bottom: 1px solid var(--surface1);
}
h3 { font-size: 1.15rem; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
p { margin: 0 0 1rem; }
.lede { color: var(--subtext0); max-width: 68ch; }
.meta { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); }
code, .mono { font-family: var(--mono); font-size: .85em; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .75rem; margin: 1.75rem 0 0; }
.kpi { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: .9rem 1rem; }
.kpi .n { font-size: 1.9rem; font-weight: 650; line-height: 1.1; font-variant-numeric: tabular-nums; }
.kpi .l { font-size: .78rem; color: var(--overlay0); text-transform: uppercase; letter-spacing: .05em; margin-top: .15rem; }
.n.good { color: var(--green); } .n.warn { color: var(--yellow); }
.n.bad { color: var(--red); } .n.info { color: var(--blue); } .n.alt { color: var(--mauve); }

.hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 1.25rem; }
.host { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 12px; overflow: hidden; }
.host-head { padding: 1.1rem 1.25rem; border-bottom: 1px solid var(--surface0); background: var(--crust); }
.host-head .row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.host-sub { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); margin-top: .3rem; }
.host-body { padding: .5rem .6rem 1rem; }

.svc { display: flex; align-items: flex-start; gap: .7rem; padding: .5rem .65rem; border-radius: 8px; }
.svc:hover { background: var(--surface0); }
.svc + .svc { border-top: 1px solid rgba(69,71,90,.45); }
.svc-main { flex: 1; min-width: 0; }
.svc-name { font-weight: 550; font-family: var(--mono); font-size: .92rem; }
.svc-detail { color: var(--overlay0); font-size: .8rem; overflow-wrap: anywhere; }
.svc-tags { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .25rem; }

.dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto; margin-top: .45rem; box-shadow: 0 0 0 2px var(--mantle); }
.dot.healthy { background: var(--green); } .dot.degraded { background: var(--yellow); }
.dot.down, .dot.missing { background: var(--red); }
.dot.job { background: var(--teal); } .dot.companion { background: var(--blue); }
.dot.undeclared { background: var(--mauve); } .dot.unknown { background: var(--overlay0); }

.tag { font-size: .68rem; font-family: var(--mono); padding: .1rem .4rem; border-radius: 4px;
       background: var(--surface0); color: var(--subtext0); border: 1px solid var(--surface1); }
.tag.auth { background: var(--lavender); color: var(--crust); border-color: var(--lavender); }
.tag.route { background: var(--blue); color: var(--crust); border-color: var(--blue); }
.tag.net { background: transparent; color: var(--teal); border-color: var(--surface2); }
.tag.ns { background: transparent; color: var(--peach); border-color: var(--surface2); }
.tag.node { background: transparent; color: var(--lavender); border-color: var(--surface2); }

.legend { display: flex; gap: 1.1rem; flex-wrap: wrap; margin: 1rem 0 0; font-size: .82rem; color: var(--subtext0); }
.legend span { display: flex; align-items: center; gap: .4rem; }
.legend .dot { margin-top: 0; }

.warn-box { background: rgba(243,139,168,.1); border: 1px solid var(--red); border-radius: 8px;
            padding: .7rem .9rem; margin: .75rem 0 0; color: var(--text); font-size: .87rem; }

figure.diagram { margin: 0; }
.dg { width: 100%; height: auto; display: block; }
.dg .box { fill: var(--mantle); stroke: var(--surface2); stroke-width: 1.4; }
.dg .plane { fill: rgba(49,50,68,.3); stroke: var(--surface1); stroke-width: 1.2; stroke-dasharray: 6 5; }
.dg .box.s-healthy { stroke: var(--green); }
.dg .box.s-degraded { stroke: var(--yellow); }
.dg .box.s-down, .dg .box.s-missing { stroke: var(--red); }
.dg .box.s-job { stroke: var(--teal); }
.dg .box.s-unknown { stroke: var(--overlay0); stroke-dasharray: 4 3; }
.dg .t-title { fill: var(--text); font-size: 13px; font-weight: 550;
                font-family: system-ui, -apple-system, sans-serif; }
.dg .t-sub { fill: var(--subtext0); font-size: 11px; font-family: var(--mono); }
.dg .t-lane { fill: var(--overlay0); font-size: 11px; letter-spacing: .1em;
              text-transform: uppercase; font-weight: 600;
              font-family: system-ui, -apple-system, sans-serif; }
.dg .t-edge { fill: var(--overlay0); font-size: 10.5px; font-family: var(--mono); }
.dg .edge { stroke: var(--surface2); stroke-width: 1.5; fill: none; }
.dg .edge.bypass { stroke: var(--peach); stroke-dasharray: 7 5; }
.dg .t-edge.bypass { fill: var(--peach); }
.dg .arrowhead { fill: var(--surface2); }
figcaption { color: var(--overlay0); font-size: .84rem; margin-top: .9rem; max-width: 78ch; }

.grps { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1rem; }
.grp { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: .9rem 1rem; }
.grp h4 { margin: 0 0 .6rem; font-size: .9rem; font-weight: 600; display: flex;
          justify-content: space-between; gap: .5rem; }
.grp-n { color: var(--overlay0); font-family: var(--mono); font-size: .8rem; font-weight: 400; }
.grp ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: .3rem; }
.grp li { display: flex; align-items: center; gap: .45rem; font-family: var(--mono); font-size: .8rem; }
.grp li .dot { margin-top: 0; }

table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--surface0); vertical-align: top; }
th { color: var(--overlay0); font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
td.mono { font-family: var(--mono); }
.scroll { overflow-x: auto; }
details > summary { cursor: pointer; color: var(--subtext0); font-size: .9rem; padding: .4rem 0; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--surface0);
         color: var(--overlay0); font-size: .82rem; }
"""

STATUS_LABELS = {
    "healthy": "Healthy",
    "degraded": "Degraded",
    "down": "Down",
    "missing": "Missing",
    "job": "Scheduled job",
    "companion": "Role companion",
    "undeclared": "Undeclared",
    "unknown": "Unknown",
}


def e(value: Any) -> str:
    """Escape a value for HTML text content."""
    return html.escape("" if value is None else str(value))


def _service_row(service: dict) -> str:
    status = service["status"]
    tags = []
    if service["hostname"]:
        target = service["hostname"]
        tags.append(f'<span class="tag route">{e(target)}:{e(service["port"])}</span>')
    elif service["port"]:
        tags.append(f'<span class="tag">:{e(service["port"])}</span>')
    if service["authelia"]:
        tags.append('<span class="tag auth">SSO</span>')
    if service.get("namespace"):
        tags.append(f'<span class="tag ns">ns/{e(service["namespace"])}</span>')
    for node in service.get("nodes") or []:
        tags.append(f'<span class="tag node">{e(node)}</span>')
    for net in service["networks"]:
        tags.append(f'<span class="tag net">{e(net)}</span>')
    if not service["declared"]:
        tags.append('<span class="tag">not in inventory</span>')

    detail = service["detail"] or STATUS_LABELS.get(status, status)
    return (
        f'<div class="svc">'
        f'<span class="dot {e(status)}" title="{e(STATUS_LABELS.get(status, status))}"></span>'
        f'<div class="svc-main">'
        f'<div class="svc-name">{e(service["name"])}</div>'
        f'<div class="svc-detail">{e(STATUS_LABELS.get(status, status))} &middot; {e(detail)}</div>'
        f'<div class="svc-tags">{"".join(tags)}</div>'
        f"</div></div>"
    )


def _host_panel(host: dict) -> str:
    counts = host["counts"]
    summary_bits = [
        f"{counts.get(key, 0)} {STATUS_LABELS[key].lower()}"
        for key in ("healthy", "degraded", "down", "missing", "undeclared", "unknown")
        if counts.get(key)
    ]
    platform_label = host["role"] or (
        "k3s / Kubernetes" if host["platform"] == "k8s" else "Docker Compose"
    )
    node = host.get("node")
    if host["platform"] == "k8s":
        # Say what is *running here*, not what declares it — the inventory
        # declares every k8s service under daniel-box, and a "0 declared" line
        # on daniel-server reads as an idle box while it carries half the pods.
        scope = f"{len(host['services'])} services running here"
        if node:
            scope += f" &middot; {node['pods']} pods"
    else:
        scope = f"{host['declared_count']} declared"
    warn = ""
    if not host["reachable"]:
        warn = (
            f'<div class="warn-box"><strong>Live state unavailable</strong> — showing '
            f"declared inventory only. {e(host['error'])}</div>"
        )
    rows = "".join(_service_row(s) for s in host["services"])
    return (
        f'<section class="host"><div class="host-head">'
        f'<div class="row"><h3>{e(host["name"])}</h3>'
        f'<span class="meta">{e(platform_label)}</span></div>'
        f'<div class="host-sub">{e(host["ip"])} &middot; {scope} '
        f"&middot; {host['routed_count']} routed &middot; {host['authelia_count']} SSO-gated</div>"
        f'<div class="host-sub">{" &middot; ".join(e(bit) for bit in summary_bits)}</div>'
        f"{warn}</div>"
        f'<div class="host-body">{rows}</div></section>'
    )


# Functional grouping for the workload strip under the diagram. This is the one
# piece of the page that is a hand-kept list rather than derived, so anything
# unlisted falls into "Other" and stays visible — a new service shows up as
# ungrouped instead of silently vanishing from the page.
SERVICE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Edge & identity",
        ("traefik", "authelia", "crowdsec", "cloudflare-ddns", "pihole", "headlamp"),
    ),
    (
        "Media",
        (
            "jellyfin",
            "sonarr",
            "radarr",
            "bazarr",
            "prowlarr",
            "qbittorrent",
            "tdarr",
            "configarr",
            "janitorr",
            "media-volume",
            "seed-volume",
        ),
    ),
    (
        "Home automation",
        ("home-assistant", "zigbee2mqtt", "mosquitto", "ical-proxy", "peanut", "nut"),
    ),
    (
        "Observability",
        (
            "uptime-kuma",
            "loki-homelab",
            "claude-otel",
            "node-exporter",
            "scrutiny",
            "monitor-bridge",
            "autofix-bridge",
            "healthchecks",
            "speedtest",
            "rollout-drain",
        ),
    ),
    (
        "Apps & tooling",
        (
            "freshrss",
            "karakeep",
            "littlelink",
            "bento-pdf",
            "homepage",
            "n8n",
            "n8n-images",
            "code-server",
            "livesync",
            "homelab-mcp",
            "registry",
            "image-builder",
        ),
    ),
    ("Games", ("terraria", "terraria-stats", "valheim", "valheim-stats")),
    (
        "Storage & backup",
        ("longhorn-ui", "pi-peer-backup", "dri-device-plugin"),
    ),
)


def group_services(model: dict) -> list[dict]:
    """Bucket every service into a functional group for the diagram strip."""
    by_group: dict[str, list[dict]] = {name: [] for name, _ in SERVICE_GROUPS}
    by_group["Pi · LAN-only"] = []
    by_group["Other"] = []
    lookup = {name: group for group, names in SERVICE_GROUPS for name in names}

    for service in model["services"]:
        if service["platform"] == "docker":
            by_group["Pi · LAN-only"].append(service)
        else:
            by_group[lookup.get(service["name"], "Other")].append(service)

    groups = []
    for name, services in by_group.items():
        if not services:
            continue
        groups.append(
            {
                "name": name,
                "services": sorted(services, key=lambda s: s["name"]),
                "healthy": sum(1 for s in services if s["status"] == "healthy"),
            }
        )
    return groups


def _svg_box(
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    subtitle: str = "",
    status: str = "",
    css: str = "",
) -> str:
    """One labelled node of the diagram, optionally tinted by live status."""
    cx = x + w // 2
    classes = " ".join(
        part for part in ("box", css, f"s-{status}" if status else "") if part
    )
    if subtitle:
        title_y, sub_y = y + h // 2 - 3, y + h // 2 + 15
        text = (
            f'<text class="t-title" x="{cx}" y="{title_y}" text-anchor="middle">{e(title)}</text>'
            f'<text class="t-sub" x="{cx}" y="{sub_y}" text-anchor="middle">{e(subtitle)}</text>'
        )
    else:
        text = f'<text class="t-title" x="{cx}" y="{y + h // 2 + 5}" text-anchor="middle">{e(title)}</text>'
    return f'<rect class="{classes}" x="{x}" y="{y}" width="{w}" height="{h}" rx="9"/>{text}'


def _svg_edge(
    points: str, label: str = "", label_xy: tuple[int, int] = (0, 0), css: str = ""
) -> str:
    classes = " ".join(part for part in ("edge", css) if part)
    edge = (
        f'<polyline class="{classes}" points="{points}" marker-end="url(#dg-arrow)"/>'
    )
    if label:
        x, y = label_xy
        label_css = "t-edge bypass" if "bypass" in css else "t-edge"
        edge += f'<text class="{label_css}" x="{x}" y="{y}">{e(label)}</text>'
    return edge


def _service_status(model: dict, name: str) -> str:
    """Live status of one service by name, for tinting a diagram box."""
    for host in model["hosts"]:
        for service in host["services"]:
            if service["name"] == name:
                return service["status"]
    return "unknown"


def _diagram_view(model: dict) -> str:
    """The architecture figure: how a request reaches a workload, and on what.

    The shape is fixed — these edges live in role templates and Traefik
    middleware, not in ``containers_list``, so they cannot be derived. Every
    label, address, count and status colour on it is read from the model.
    """
    ep = model["endpoints"]
    cluster = model["cluster"]
    box = next((h for h in model["hosts"] if h["name"] == "daniel-box"), None)
    pi = next((h for h in model["hosts"] if h["name"] == "daniel-pi"), None)
    routed = box["routed_count"] if box else 0
    gated = box["authelia_count"] if box else 0
    domain = model["domain"] or "the domain"

    parts = [
        '<defs><marker id="dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path class="arrowhead" d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
    ]

    # Request path, top to bottom.
    parts.append(_svg_box(250, 26, 200, 48, "Internet"))
    parts.append(_svg_box(490, 26, 200, 48, "LAN clients"))
    parts.append(
        _svg_box(
            250,
            108,
            200,
            52,
            "Cloudflare DNS",
            domain,
            _service_status(model, "cloudflare-ddns"),
        )
    )
    parts.append(
        _svg_box(
            490,
            108,
            200,
            52,
            "Pi-hole → Unbound",
            f"{ep['dns_vip']}:53",
            _service_status(model, "pihole"),
        )
    )
    parts.append(
        _svg_box(
            330,
            196,
            480,
            52,
            "Router :80/:443 → MetalLB ingress VIP",
            ep["ingress_vip"],
        )
    )
    parts.append(
        _svg_box(
            330,
            278,
            480,
            58,
            "Traefik  ·  CrowdSec bouncer + AppSec WAF",
            f"{routed} routed hostnames",
            _service_status(model, "traefik"),
        )
    )
    parts.append(
        _svg_box(
            330,
            372,
            480,
            58,
            "Authelia  ·  forwardAuth SSO",
            f"{gated} of {routed} routes gated",
            _service_status(model, "authelia"),
        )
    )

    # k8s_public_route decides whether IngressRoutes *match* the public hostname,
    # not whether the router forwards 80/443 here. Say only the first.
    public_label = (
        f"routes match *.{domain}" if ep["public_routes"] else "LAN-only routes"
    )
    parts.append(_svg_edge("350,74 350,108", "A/AAAA", (356, 96)))
    parts.append(_svg_edge("590,74 590,108", "DHCP-assigned resolver", (596, 96)))
    parts.append(_svg_edge("350,160 350,178 430,178 430,196", public_label, (250, 190)))
    parts.append(
        _svg_edge("590,160 590,178 670,178 670,196", f"*.local.{domain}", (676, 190))
    )
    parts.append(_svg_edge("570,248 570,278", ":80/:443", (578, 266)))
    parts.append(_svg_edge("570,336 570,372", "forwardAuth", (578, 358)))
    parts.append(
        _svg_edge("570,430 570,470", "proxies to ClusterIP Services", (578, 454))
    )

    # The LoadBalancer VIPs that never touch Traefik — raw TCP, and the reason a
    # Traefik outage does not take Jellyfin or MQTT with it.
    parts.append(_svg_edge("690,50 1120,50 1120,540 1100,540", css="bypass"))
    parts.append(
        '<text class="t-edge bypass" x="704" y="38">LoadBalancer VIPs — bypass Traefik</text>'
    )
    parts.append(
        f'<text class="t-edge bypass" x="704" y="66">Jellyfin {e(ep["jellyfin_vip"])}'
        f" · MQTT {e(ep['mqtt_vip'])}</text>"
    )

    # Cluster plane.
    volumes = cluster["volumes"]
    plane_sub = f"{cluster['pod_count']} pods"
    if volumes is not None:
        plane_sub += f"  ·  {volumes} Longhorn volumes"
    parts.append(
        '<rect class="plane" x="40" y="470" width="1060" height="200" rx="14"/>'
    )
    parts.append('<text class="t-lane" x="62" y="497">k3s cluster</text>')
    parts.append(
        f'<text class="t-sub" x="1078" y="497" text-anchor="end">{e(plane_sub)}</text>'
    )

    node_boxes = cluster["nodes"] or [
        {
            "name": h["name"],
            "ip": h["ip"],
            "ready": False,
            "pods": 0,
            "roles": [],
            "version": "",
        }
        for h in model["hosts"]
        if h["platform"] == "k8s"
    ]
    for index, node in enumerate(node_boxes[:2]):
        roles = ", ".join(node["roles"]) or "agent"
        # Only claim NotReady when the query actually answered. An uncollected
        # cluster and a failed node look identical in this dict, and painting the
        # second when it was the first is the false alarm that teaches a reader
        # to stop believing red.
        if not cluster["ok"]:
            state = "unknown"
        else:
            state = "healthy" if node["ready"] else "down"
        parts.append(
            _svg_box(
                70 + index * 510,
                520,
                490,
                110,
                f"{node['name']}  ·  {roles}",
                (
                    f"{node['ip']}  ·  {node['pods']} pods  ·  {node['version']}"
                    if cluster["ok"]
                    else f"{node['ip']}  ·  not collected"
                ),
                state,
            )
        )

    # Storage and the backup chain. Tinted by whether the volumes could be read,
    # not by the longhorn-ui service: that entry declares only an IngressRoute,
    # its Deployment belongs to the Longhorn chart in longhorn-system, and the
    # name lookup in ns/homelab therefore misses it and reports "missing". A
    # healthy storage plane must not read as red because a route lookup missed.
    parts.append(
        _svg_box(
            40,
            706,
            330,
            76,
            "Longhorn",
            f"ns/{ep['longhorn_namespace']}"
            + (f"  ·  {volumes} volumes" if volumes is not None else ""),
            "healthy" if volumes is not None else "unknown",
        )
    )
    # Same rule as the nodes, and it matters more here: "disarmed" is a real and
    # deliberate state in this repo, so a failed query must not be able to
    # announce it. No targets collected means unknown, not unarmed.
    targets = cluster["backup_targets"] or [
        {"name": "default", "url": "", "armed": False, "available": False}
    ]
    for index, target in enumerate(targets[:2]):
        if not cluster["ok"] or not cluster["backup_targets"]:
            state, detail = "unknown", "not collected"
        elif not target["armed"]:
            state, detail = "missing", "disarmed — no backup target URL"
        elif target["available"]:
            state, detail = "healthy", target["url"]
        else:
            state, detail = "down", f"unavailable — {target['url']}"
        parts.append(
            _svg_box(
                440,
                690 + index * 80,
                300,
                60,
                f"BackupTarget/{target['name']}",
                detail,
                state,
            )
        )
        parts.append(
            _svg_edge(f"370,744 405,744 405,{720 + index * 80} 440,{720 + index * 80}")
        )

    # The Pi: its own plane, reached from the LAN and never through the cluster edge.
    pi_services = ", ".join(s["name"] for s in (pi["services"] if pi else [])) or "none"
    parts.append(
        '<rect class="plane" x="790" y="690" width="310" height="190" rx="14"/>'
    )
    parts.append('<text class="t-lane" x="812" y="717">daniel-pi · Docker</text>')
    parts.append(
        f'<text class="t-sub" x="812" y="740">{e(pi["ip"] if pi else "")} · LAN-only, no Traefik route</text>'
    )
    parts.append(
        '<text class="t-sub" x="812" y="766">WireGuard peers → wg-easy :51820/udp</text>'
    )
    # Four lines is what fits inside the plane; a longer list is elided rather
    # than drawn past the edge, and the host panel below carries all of it.
    lines = _wrap(pi_services, 34)
    if len(lines) > 4:
        lines = lines[:3] + [f"… +{len(lines) - 3} more lines"]
    for index, chunk in enumerate(lines):
        parts.append(
            f'<text class="t-title" x="812" y="{800 + index * 20}">{e(chunk)}</text>'
        )

    caption = (
        "How a request reaches a workload, and what it runs on. Box outlines carry live "
        "status; every address, hostname and count is read from the inventory and the "
        "cluster at render time."
    )
    return (
        '<figure class="diagram">'
        f'<svg class="dg" viewBox="0 0 1140 900" role="img" aria-label="{e(caption)}">'
        f"{''.join(parts)}</svg>"
        f"<figcaption>{e(caption)}</figcaption></figure>"
    )


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap — SVG text has no flow, so lines are placed by hand."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _groups_view(model: dict) -> str:
    """A status-coloured chip per service, grouped by what it is for."""
    columns = []
    for group in group_services(model):
        chips = "".join(
            f'<li><span class="dot {e(s["status"])}" title="{e(STATUS_LABELS.get(s["status"], s["status"]))}">'
            f"</span>{e(s['name'])}</li>"
            for s in group["services"]
        )
        columns.append(
            f'<div class="grp"><h4>{e(group["name"])} '
            f'<span class="grp-n">{group["healthy"]}/{len(group["services"])}</span></h4>'
            f"<ul>{chips}</ul></div>"
        )
    return f'<div class="grps">{"".join(columns)}</div>'


def _table_view(model: dict) -> str:
    rows = []
    for service in model["services"]:
        # "Runs on" is where the pods landed; a k8s service with none falls back
        # to the host that declares it.
        runs_on = ", ".join(service.get("nodes") or []) or service.get("host", "")
        rows.append(
            "<tr>"
            f'<td class="mono">{e(service["name"])}</td>'
            f'<td class="mono">{e(runs_on)}</td>'
            f"<td>{e(service['platform'])}</td>"
            f"<td>{e(STATUS_LABELS.get(service['status'], service['status']))}</td>"
            f'<td class="mono">{e(service["hostname"] or "—")}</td>'
            f'<td class="mono">{e(service["image"] or "—")}</td>'
            f"<td>{e(service['detail'] or '—')}</td>"
            "</tr>"
        )
    return (
        "<details><summary>Full service table (sortable by eye, copy-pasteable)</summary>"
        '<div class="scroll"><table><thead><tr>'
        "<th>Service</th><th>Runs on</th><th>Platform</th><th>Status</th>"
        "<th>Hostname</th><th>Image</th><th>Detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def render_html(model: dict) -> str:
    """Render the model to a single self-contained HTML document."""
    totals = model["totals"]
    kpis = [
        ("info", totals["services"], "Services"),
        ("good", totals["healthy"], "Healthy"),
        ("warn", totals["degraded"], "Degraded"),
        ("bad", totals["down"], "Down / missing"),
        ("alt", totals["undeclared"], "Undeclared"),
        ("info", model["cluster"]["pod_count"], "Pods running"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n {css}">{value}</div><div class="l">{e(label)}</div></div>'
        for css, value, label in kpis
    )
    legend = "".join(
        f'<span><span class="dot {key}"></span>{label}</span>'
        for key, label in STATUS_LABELS.items()
    )
    hosts_html = "".join(_host_panel(h) for h in model["hosts"])

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<title>Homelab infrastructure — daniel-box &amp; daniel-server</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<h1>Homelab infrastructure</h1>
<p class="lede">Declared state from <code>ansible/inventory/host_vars/</code>, overlaid with
live state from <code>kubectl</code> and <code>docker ps</code>. Regenerated on a timer, so
edits to the inventory and drift in the running fleet both show up here on their own.</p>
<p class="meta">Generated {e(model["generated_at"])} &middot; page reloads every {PAGE_REFRESH_SECONDS // 60} min</p>
<div class="kpis">{kpi_html}</div>
<div class="legend">{legend}</div>

<h2>Architecture</h2>
{_diagram_view(model)}

<h2>Workloads by function</h2>
{_groups_view(model)}

<h2>Hosts</h2>
<div class="hosts">{hosts_html}</div>

<h2>All services</h2>
{_table_view(model)}

<footer>
Sources: <code>ansible/inventory/</code> for declared state; live state from
<code>kubectl</code> on daniel-box (deployments, nodes, pods, Longhorn volumes and
backup targets) and one <code>docker ps</code> over ssh to daniel-pi.
The diagram's <em>shape</em> is fixed in <code>scripts/gen_infra_map.py</code> — those edges
live in role templates, not in the inventory — while its labels, counts and status
colours are read at render time.
Regenerate with <code>uv run python scripts/gen_infra_map.py</code>.
</footer>
</div></body></html>
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def collect_live(
    local_hostname: str, longhorn_namespace: str
) -> tuple[dict[str, dict], dict]:
    """Gather live state for every host, tolerating failures per host.

    The two k3s hosts share one collection: workloads come from the cluster, not
    from either box individually, and a k3s host is reachable when its Node is
    Ready — not when it answers ssh. Only the Pi is polled over ssh, in a single
    call, because `ufw limit ssh` rejects a chatty caller.
    """
    cluster = collect_cluster(local_hostname, longhorn_namespace)
    deployments: dict = {}
    deployments_ok, deployments_err = False, cluster["error"]
    if cluster["ok"]:
        deployments_ok, deployments, deployments_err = collect_k8s(
            local_hostname, local_hostname
        )

    live: dict[str, dict] = {}
    for host in HOSTS:
        if HOST_PLANE[host] == "docker":
            ok, data, err = collect_docker(host, local_hostname)
            live[host] = {"ok": ok, "data": data, "error": err}
            continue
        node = cluster["nodes"].get(host)
        if not deployments_ok:
            err = deployments_err
        elif node is None:
            err = f"{host} is not a member of the cluster"
        elif not node["ready"]:
            err = f"node {host} is not Ready"
        else:
            err = ""
        live[host] = {"ok": not err, "data": deployments, "error": err}
    return live, cluster


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="output HTML path"
    )
    parser.add_argument(
        "--no-live", action="store_true", help="render declared inventory only"
    )
    parser.add_argument(
        "--json", action="store_true", help="dump the model as JSON instead"
    )
    args = parser.parse_args(argv)

    global_vars, host_vars = load_inventory()
    local_hostname = socket.gethostname()
    longhorn_namespace = global_vars.get("k8s_longhorn_namespace", "longhorn-system")
    try:
        if args.no_live:
            live = {h: {"ok": False, "data": {}, "error": "--no-live"} for h in HOSTS}
            cluster = None
        else:
            live, cluster = collect_live(local_hostname, longhorn_namespace)
    except MissingToolError as exc:
        # Deliberately leave the existing page alone. Overwriting it with a
        # declared-only render would replace real data with a page that looks
        # healthy and reports nothing wrong.
        print(f"error: {exc}; leaving the previous map in place", file=sys.stderr)
        return 2
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    model = build_model(
        global_vars, host_vars, live, generated_at, load_roles(), cluster
    )

    if args.json:
        json.dump(model, sys.stdout, indent=2)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a reader never sees a half-written page.
    tmp = args.output.with_suffix(".html.tmp")
    tmp.write_text(render_html(model), encoding="utf-8")
    os.replace(tmp, args.output)

    unreachable = [h["name"] for h in model["hosts"] if not h["reachable"]]
    note = (
        f" (live state unavailable for: {', '.join(unreachable)})"
        if unreachable
        else ""
    )
    print(f"Wrote {args.output}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
