#!/usr/bin/env python3
"""The service catalogue's row type, its "cannot be derived" marker, and its path anchors.

Split out of ``scripts/docs/service_catalog.py`` on 2026-09-04. Every other catalogue module
reads ``UNKNOWN`` or a path constant from here, so this is the one leaf with no first-party
dependency beyond ``lib.repo_paths`` — which is what keeps the derivation modules from having
to import the generator they were split out of.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dataclasses import dataclass

from lib.repo_paths import REPO

__all__ = [
    "K3S_DEFAULTS",
    "K8S_ROLES",
    "UNKNOWN",
    "ServiceRow",
]


K8S_ROLES = REPO / "ansible" / "roles" / "k8s"
K3S_DEFAULTS = REPO / "ansible" / "roles" / "setup" / "k3s" / "defaults" / "main.yml"

UNKNOWN = "unknown"


@dataclass
class ServiceRow:
    """One row of the service catalog: a single service's derived facts, host-scoped.

    Attributes:
        route: The IngressRoute/Traefik reachability derivation, or an `UNKNOWN` explanation.
        auth_tier: Whether the route sits behind Authelia, read from `use_authelia`.
        backup_tier: The Longhorn backup tier(s) its PVC(s) fall into, or "n/a" off k8s.
        autodeploy: Whether the GitOps deployer will auto-apply this service's image bumps.
    """

    name: str
    host: str
    platform: str  # "k8s" or "docker"
    route: str
    auth_tier: str
    backup_tier: str
    autodeploy: str
