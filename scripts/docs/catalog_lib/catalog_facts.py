#!/usr/bin/env python3
"""Route and auth-tier derivations for one ``containers_list`` entry.

Reachability itself comes from ``route_facts`` so this page and
``docs/reference/networking.md`` cannot disagree about the same service; what lives here is
what each cell says, including when no rule applies.
"""

from pathlib import Path
from typing import Any

from docs.catalog_lib.catalog_model import K8S_ROLES, UNKNOWN
from lib.k8s_roles import manifest_template
from lib.estate import REPO_INVENTORY, Inventory
from docs.catalog_lib.route_facts import reachability, route_cell

__all__ = [
    "auth_tier",
    "k8s_route",
]


# Route


def k8s_route(
    entry: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    inventory: Inventory = REPO_INVENTORY,
) -> str:
    """Derive a k8s service's route cell from its role's IngressRoute template, if any.

    Args:
        entry: The service's `containers_list` entry.
        k8s_roles: Root directory of the k8s roles.
        inventory: The inventory whose group variables hold the public-route default.

    Returns:
        A route cell string, or "no route (infra role)" when the role has no IngressRoute.
    """
    name = entry["name"]
    role_dir = k8s_roles / name
    # Own template or the shared default: asking the role directory alone printed 'no route' for
    # roles using `ingressroute-default.yaml.j2`.
    if manifest_template(name, "ingressroute.yaml", k8s_roles, entry) is None:
        return "no route (infra role)"
    label = entry.get("hostname") or name
    # Reachability comes from route_facts so this page and networking.md cannot disagree.
    return route_cell(label, reachability(role_dir, inventory, entry))


# `use_authelia` is a direct field read; `auth_tier` is what the authelia role renders into
# access_control.


def auth_tier(entry: dict[str, Any]) -> str:
    if "use_authelia" not in entry:
        return UNKNOWN + " (use_authelia not declared on this entry)"
    if not entry["use_authelia"]:
        return "none (public/no-auth)"
    policy = entry.get("auth_tier")
    return f"Authelia {policy}" if policy else "Authelia"
