"""Declared state: what ``containers_list`` and the role trees say should run.

Reads the repo only — no cluster, no ssh. ``infra_map_live`` gathers what is
actually running, and ``infra_map_model`` reconciles the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from infra_map_common import (
    _CONTAINER_NAME,
    _JINJA_VAR,
    _MANIFEST_KIND,
    HOSTS,
    NAMESPACE_OWNERS,
    REPO_ROOT,
)


# The kinds the live collector reads; a role declaring none of them is a batch
# role. Kept in step with ``infra_map_live.WORKLOAD_KINDS`` by a test.
LONG_RUNNING_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})

# A ``namespace:`` line whose value is a bare name. A Jinja value fails the
# character class, so ``{{ k8s_namespace }}`` is deliberately not captured: the
# inventory already resolves that one, and this only records the exceptions.
_LITERAL_NAMESPACE = re.compile(
    r"^\s+namespace:\s*([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE
)


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
    # Namespaces a k8s role's manifests name literally, rather than through the
    # ``k8s_namespace`` variable the inventory resolves for it. The map looks a
    # role's workloads up by namespace, so a role that places one elsewhere on
    # purpose (dri-device-plugin's DaemonSet in kube-system) is found only here.
    manifest_namespaces: dict[str, frozenset[str]] = field(default_factory=dict)


def load_roles(repo_root: Path = REPO_ROOT) -> RoleIndex:
    """Build the role index by reading the role trees, not by guessing names."""
    owners: dict[str, str] = {}
    batch: set[str] = set()
    manifest_namespaces: dict[str, frozenset[str]] = {}

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

    # A k8s role that declares no long-running workload has nothing to find
    # between deploys, so "not running" is correct for it, not a fault. That is
    # a role with no templates directory (the seed and snapshot roles), one whose
    # only workload is a CronJob, and one whose templates are all non-workload
    # objects: Dockerfiles (n8n-images), an IngressRoute onto a chart-owned
    # Deployment (longhorn-ui), a PVC (media-volume), NetworkPolicies
    # (netpol-baseline). Derived from the manifests rather than from the Docker
    # compose that used to declare no container name — that plumbing was deleted
    # with the migration.
    k8s_roles = repo_root / "ansible" / "roles" / "k8s"
    for role in sorted(p for p in k8s_roles.glob("*") if p.is_dir()):
        templates = role / "templates"
        if not templates.is_dir():
            batch.add(role.name)
            continue
        kinds: set[str] = set()
        literal_namespaces: set[str] = set()
        for tpl in templates.glob("*.yaml.j2"):
            text = tpl.read_text()
            kinds.update(_MANIFEST_KIND.findall(text))
            literal_namespaces.update(_LITERAL_NAMESPACE.findall(text))
        if not kinds & LONG_RUNNING_KINDS:
            batch.add(role.name)
        if literal_namespaces:
            manifest_namespaces[role.name] = frozenset(literal_namespaces)

    return RoleIndex(
        container_owners=owners,
        batch_roles=frozenset(batch),
        manifest_namespaces=manifest_namespaces,
    )


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
