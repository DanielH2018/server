#!/usr/bin/env python3
"""Route and auth-tier derivations for one ``containers_list`` entry.

Split out of ``scripts/docs/service_catalog.py`` on 2026-09-04. Reachability itself comes from
``route_facts`` so this page and ``docs/reference/networking.md`` cannot disagree about the
same service; what lives here is the per-entry dispatch — which platform's rule applies, and
what the cell says when no rule does.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path
from typing import Any

from catalog_model import K8S_ROLES, UNKNOWN
from lib.render_guard import ALL_VARS
from route_facts import reachability, route_cell

__all__ = [
    "auth_tier",
    "docker_route",
    "host_expose_mode",
    "k8s_route",
    "route_for",
]


def host_expose_mode(host_data: dict[str, Any]) -> str | None:
    return host_data.get("expose_mode")


# Route


def k8s_route(
    entry: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> str:
    """Derive a k8s service's route cell from its role's IngressRoute template, if any.

    Args:
        entry: The service's `containers_list` entry.
        k8s_roles: Root directory of the k8s roles.
        all_vars: Path to `group_vars/all.yml`, read for the public-route default.

    Returns:
        A route cell string, or "no route (infra role)" when the role has no IngressRoute.
    """
    name = entry["name"]
    role_dir = k8s_roles / name
    if not (role_dir / "templates" / "ingressroute.yaml.j2").is_file():
        return "no route (infra role)"
    # ingressroute.yml.j2's own macro call is uniformly
    # `container_item.hostname | default(container_item.name)` — see the shared macro
    # docstring at ansible/templates/ingressroute.yml.j2.
    label = entry.get("hostname") or name
    # Reachability comes from route_facts so this page and networking.md cannot disagree
    # about the same service. It reads the role's own `public=false` and the cluster-wide
    # k8s_public_route together, rather than hedging with "if k8s_public_route" — that flag
    # has a value in plaintext group_vars, so printing the condition instead of the answer
    # made the reader do a lookup this generator can do for them.
    return route_cell(label, reachability(role_dir, all_vars))


def docker_route(entry: dict[str, Any], host_data: dict[str, Any]) -> str:
    if host_expose_mode(host_data) == "lan":
        return "LAN-direct (no Traefik route)"
    return UNKNOWN + " (docker route derivation only handles expose_mode: lan)"


def route_for(
    entry: dict[str, Any],
    platform: str,
    host_data: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> str:
    """Derive `entry`'s route cell, dispatching to `k8s_route` or `docker_route` by platform."""
    if platform == "k8s":
        return k8s_route(entry, k8s_roles, all_vars)
    return docker_route(entry, host_data)


# Auth tier — containers_list.use_authelia is read directly by the IngressRoute macro
# (`container_item.use_authelia`) and by the docker traefik.yml.j2 macro alike, so this
# is a direct field read, not an inference from the route template.


def auth_tier(entry: dict[str, Any]) -> str:
    if "use_authelia" not in entry:
        return UNKNOWN + " (use_authelia not declared on this entry)"
    return "Authelia" if entry["use_authelia"] else "none (public/no-auth)"
