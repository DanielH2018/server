"""Live state: what the cluster and the Pi report is actually running.

Every collector here degrades to "unreachable" rather than raising, because this
runs unattended and a partial map beats no map. ``infra_map.inventory`` supplies
the declared skeleton this is overlaid onto.
"""

import json
import subprocess
import sys as _sys
from pathlib import Path as _Path

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script gets only its own directory,
# and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.constants import HOST_PLANE, LOCAL_TIMEOUT, SSH_TIMEOUT

# Tool discovery lives with the kubectl invoker because kubectl is what cron blinds first:
# its PATH omits /usr/local/bin (kubectl's home, as a symlink to k3s) and carries no
# KUBECONFIG, so k3s falls back to the root-owned kubeconfig and returns nothing. The same
# `find_tool` covers docker and ssh below.
from lib import kubectl as kubectl_lib
from lib.kubectl import MissingKubectl, WrongCluster, cluster_for_host, find_tool


class MissingToolError(Exception):
    """A collector binary is absent — a broken setup, not a host being down.

    Kept distinct from an unreachable host on purpose. A host that is down is an
    observation worth rendering; a missing binary means this run could never
    have seen anything, and a page that quietly reports declared-only in that
    case looks identical to a healthy one. This escalates to a non-zero exit so
    cron surfaces it instead of overwriting the page with a half-blind copy.
    """


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


def _kubectl(
    cluster: str, args: list[str], timeout: int, tools: kubectl_lib.Tools
) -> tuple[bool, str]:
    """``_run`` for kubectl: ``lib.kubectl.kubectl`` under the same ``(ok, out)`` contract.

    A wrong-cluster refusal is an observation (``ok=False``, the refusal as the error), so
    the page renders it rather than going half-blind. A missing binary or kubeconfig is a
    broken setup and escalates to ``MissingToolError`` like the docker and ssh collectors.
    """
    try:
        proc = kubectl_lib.kubectl(cluster, *args, timeout=timeout, tools=tools)
    except MissingKubectl as exc:
        raise MissingToolError(str(exc)) from exc
    except (WrongCluster, subprocess.TimeoutExpired, OSError) as exc:
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


# The three long-running workload kinds. A DaemonSet has no spec.replicas: its
# desired count is how many nodes the scheduler picked, which only the status
# carries. Reading spec.replicas for every kind is how DaemonSet-backed roles
# (node-exporter, dri-device-plugin) read as "missing" behind 2/2 ready pods.
WORKLOAD_KINDS = ("deployments", "daemonsets", "statefulsets")


def parse_kubectl_workloads(payload: str) -> dict[tuple[str, str], dict]:
    """Parse ``kubectl get deployments,daemonsets,statefulsets -A -o json``.

    Returns ``{(ns, name): info}``. Each item carries its own ``kind`` because
    a multi-resource ``get`` returns a ``List`` of typed objects; a payload from
    a plain ``get deployments`` omits it, and is read as Deployments.
    """
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
        kind = item.get("kind", "Deployment")
        if kind == "DaemonSet":
            desired = status.get("desiredNumberScheduled", 0) or 0
            ready = status.get("numberReady", 0) or 0
        else:
            desired = spec.get("replicas", 0)
            ready = status.get("readyReplicas", 0) or 0
        containers = (
            item.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        workloads[(namespace, name)] = {
            "kind": kind,
            "ready": ready,
            "desired": desired,
            "image": containers[0].get("image", "") if containers else "",
        }
    return workloads


def collect_k8s(
    host: str, local_hostname: str, tools: kubectl_lib.Tools = kubectl_lib.DEFAULT_TOOLS
) -> tuple[bool, dict, str]:
    """Collect long-running workload state from the cluster (local kubectl only)."""
    if host != local_hostname:
        return False, {}, f"kubectl only queried locally; run this on {host}"
    cluster = cluster_for_host(local_hostname)
    if cluster is None:
        return False, {}, f"{local_hostname} is a node of no known cluster"
    ok, out = _kubectl(
        cluster,
        ["get", ",".join(WORKLOAD_KINDS), "-A", "-o", "json"],
        LOCAL_TIMEOUT,
        tools,
    )
    if not ok:
        return False, {}, out
    return True, parse_kubectl_workloads(out), ""


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
        # A label key is `<group>/<name>`, so compare the group exactly rather than testing
        # the key for a `<group>/` prefix. Identical for well-formed keys, and CodeQL reads a
        # prefix test against a dotted string as URL-host sanitization (py/incomplete-url-
        # substring-sanitization), which it is not.
        roles = sorted(
            name
            for group, sep, name in (
                label.partition("/") for label in meta.get("labels", {})
            )
            if sep and group == "node-role.kubernetes.io"
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


def collect_cluster(
    local_hostname: str,
    longhorn_namespace: str,
    tools: kubectl_lib.Tools = kubectl_lib.DEFAULT_TOOLS,
) -> dict:
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

    cluster = cluster_for_host(local_hostname)
    if cluster is None:
        return {**empty, "error": f"{local_hostname} is a node of no known cluster"}

    ok, out = _kubectl(cluster, ["get", "nodes", "-o", "json"], LOCAL_TIMEOUT, tools)
    if not ok:
        return {**empty, "error": out}
    nodes = parse_kubectl_nodes(out)

    ok, out = _kubectl(
        cluster,
        ["get", "pods", "-A", "--no-headers", "-o", f"custom-columns={POD_COLUMNS}"],
        LOCAL_TIMEOUT,
        tools,
    )
    pods = parse_pod_placement(out) if ok else []

    ok, out = _kubectl(
        cluster,
        [
            "get",
            "volumes.longhorn.io",
            "-n",
            longhorn_namespace,
            "--no-headers",
            "-o",
            "custom-columns=NAME:.metadata.name",
        ],
        LOCAL_TIMEOUT,
        tools,
    )
    volumes = len([line for line in out.splitlines() if line.strip()]) if ok else None

    ok, out = _kubectl(
        cluster,
        ["get", "backuptargets.longhorn.io", "-A", "-o", "json"],
        LOCAL_TIMEOUT,
        tools,
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
