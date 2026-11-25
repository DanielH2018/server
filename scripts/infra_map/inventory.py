"""Declared state: what ``containers_list`` and the role trees say should run.

Reads the repo only — no cluster, no ssh. ``infra_map.live`` gathers what is
actually running, and ``infra_map.model`` reconciles the two.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from lib import yaml_fast
from lib.inventory_lists import K8S_HOST, PI_HOST, pi_entries_in
from lib.container_entry import ContainerEntry
from lib.render_guard import host_layer, k8s_services

from infra_map.constants import (
    _CONTAINER_NAME,
    _JINJA_VAR,
    _MANIFEST_KIND,
    HOSTS,
    NAMESPACE_OWNERS,
    REPO_ROOT,
)


# Kept in step with `infra_map.live.WORKLOAD_KINDS` by a test.
LONG_RUNNING_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})

# A Jinja value fails the character class on purpose: the inventory already resolves `{{
# k8s_namespace }}`; this records the exceptions.
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
    # The map looks workloads up by namespace, so a role that places one elsewhere on purpose
    # (dri-device-plugin in kube-system) is found only here.
    manifest_namespaces: dict[str, frozenset[str]] = field(default_factory=dict)


def load_roles(repo_root: Path = REPO_ROOT) -> RoleIndex:
    """Build the role index by reading the role trees, not by guessing names."""
    owners: dict[str, str] = {}
    batch: set[str] = set()
    manifest_namespaces: dict[str, frozenset[str]] = {}

    services = repo_root / "hosts" / "components" / "pi-compose" / "services"
    for service in sorted(p for p in services.glob("*") if p.is_dir()):
        compose = service / "compose.yaml.j2"
        if not compose.is_file():
            continue
        names = _CONTAINER_NAME.findall(compose.read_text())
        if not names:
            batch.add(service.name)
        for name in names:
            owners[name] = service.name

    # A role with no long-running workload has nothing to find between deploys, so 'not running' is
    # correct for it; derived from the manifests.
    k8s_roles = repo_root / "k8s" / "roles"
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


def resolve_vars[T](value: T, variables: dict[str, Any], _depth: int = 0) -> T | str:
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
) -> tuple[dict[str, Any], dict[str, dict], tuple[ContainerEntry, ...]]:
    """Return ``(global_vars, {host: host_vars}, k8s services)`` from the inventory."""
    inventory = repo_root / "inventory"
    global_vars = (
        yaml_fast.safe_load((inventory / "group_vars" / "all.yml").read_text()) or {}
    )
    host_vars = {host: host_layer(inventory / "host_vars", host) for host in HOSTS}
    return global_vars, host_vars, k8s_services(repo_root / "k8s" / "services.yml")


def declared_services(
    host: str, host_vars: dict, global_vars: dict, k8s: Sequence[dict] = ()
) -> list[dict]:
    """Flatten a host's declared services into normalized service records.

    daniel-pi's services are its ``compose_services`` (H5), on Docker; ``pi_entries_in`` raises
    when that key is missing. The k8s host's are `k8s`, the k8s service list. Any other host
    declares none.
    """
    variables = {**global_vars, **host_vars}
    services = []
    if host == PI_HOST:
        entries, platform = pi_entries_in(host_vars), "docker"
    else:
        entries, platform = (list(k8s) if host == K8S_HOST else []), "k8s"
    for entry in entries:
        name = entry["name"]
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
