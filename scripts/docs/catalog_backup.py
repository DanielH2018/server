#!/usr/bin/env python3
"""Longhorn backup tier and GitOps auto-deploy eligibility for one ``containers_list`` entry.

Split out of ``scripts/docs/service_catalog.py`` on 2026-09-04. Both facts are read from a
k8s role's own files — its PVC claim names for the tier, its ``k8s_autodeploy`` default for
eligibility — and both report a reason rather than a guess when the role does not say. The
FIELD NOTES in the generator's own docstring record which cases those are and why.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path
from typing import Any

from catalog_model import K3S_DEFAULTS, K8S_ROLES, UNKNOWN
from lib.render_guard import load_yaml as _load_yaml

__all__ = [
    "autodeploy_eligibility",
    "backup_tier",
    "load_longhorn_tier_lists",
]


# Backup tier (k8s / Longhorn only — Pi's Docker volumes are not Longhorn-backed)

_PVC_BLOCK_RE = re.compile(
    r"kind:\s*PersistentVolumeClaim.*?metadata:\s*\n\s*name:\s*(\{\{.*?\}\}|\S+)",
    re.DOTALL,
)
# Fallback for a role (home-assistant is the one seen so far) that mounts a PVC it never
# declares as its own `kind: PersistentVolumeClaim` object — the claim is provisioned
# elsewhere and only referenced by `claimName:` in a pod spec's volumes list.
_CLAIM_NAME_RE = re.compile(r"claimName:\s*(\{\{.*?\}\}|\S+)")
_SIMPLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _pvc_claim_names(role_dir: Path) -> list[str]:
    """Literal or single-variable PVC claim name expressions a k8s role depends on.

    Returns the raw expression text (e.g. "authelia-config" or "{{ media_volume_claim }}")
    for every PersistentVolumeClaim declared in the role's templates/*.j2 files, plus any
    `claimName:` reference to a PVC declared elsewhere (see _CLAIM_NAME_RE above).
    """
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    names = []
    for tmpl in sorted(templates.glob("*.j2")):
        text = tmpl.read_text()
        names.extend(_PVC_BLOCK_RE.findall(text))
        names.extend(_CLAIM_NAME_RE.findall(text))
    # De-duplicate while keeping order — a role that both declares its own PVC and
    # references it by claimName would otherwise double-count.
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _resolve_claim_name(expr: str, role_dir: Path) -> str | None:
    """Resolve a PVC name expression to a literal string.

    Returns None if it can't be resolved from this role's own defaults/main.yml alone
    (see FIELD NOTES).
    """
    if not expr.startswith("{{"):
        return expr  # already literal
    match = _SIMPLE_VAR_RE.match(expr)
    if not match:
        return None
    var = match.group(1)
    defaults = _load_yaml(role_dir / "defaults" / "main.yml")
    value = defaults.get(var)
    if isinstance(value, str) and "{{" not in value:
        return value
    return None


def load_longhorn_tier_lists(
    k3s_defaults: Path = K3S_DEFAULTS,
) -> tuple[set[str], set[str]]:
    """(r2_volumes, weekly_volumes) — "namespace/claim" strings, from the k3s role's defaults.

    Everything else with a PVC falls into the `default` RecurringJob group, which Longhorn applies
    to any volume with no job of its own (daily, to B2) — see
    ansible/roles/setup/k3s/templates/longhorn-recurringjob.yaml.j2.
    """
    data = _load_yaml(k3s_defaults)
    r2 = set(data.get("k3s_longhorn_r2_volumes") or [])
    weekly = set(data.get("k3s_longhorn_weekly_volumes") or [])
    return r2, weekly


def backup_tier(
    entry: dict[str, Any],
    platform: str,
    k8s_namespace: str,
    r2_volumes: set[str],
    weekly_volumes: set[str],
    k8s_roles: Path = K8S_ROLES,
) -> str:
    """Derive `entry`'s Longhorn backup tier(s) from its role's PVC claim names.

    Resolves each PVC the role declares (or references by `claimName:`) to a literal claim
    name, then classifies `namespace/claim` against the R2 and weekly volume lists. A role
    with multiple PVCs in different tiers reports all of them, de-duplicated.

    Args:
        entry: The service's `containers_list` entry.
        platform: "k8s" or "docker" — only "k8s" is Longhorn-backed.
        k8s_namespace: The cluster namespace PVCs are classified under.
        r2_volumes: "namespace/claim" strings backed up daily to R2.
        weekly_volumes: "namespace/claim" strings backed up weekly to B2.
        k8s_roles: Root directory of the k8s roles.

    Returns:
        A semicolon-joined string of tier labels, "no PVC (stateless)", or "n/a" off k8s.
    """
    if platform != "k8s":
        return "n/a (Docker/Pi, not Longhorn-backed)"
    role_dir = k8s_roles / entry["name"]
    claim_exprs = _pvc_claim_names(role_dir)
    if not claim_exprs:
        return "no PVC (stateless)"
    tiers = []
    for expr in claim_exprs:
        claim = _resolve_claim_name(expr, role_dir)
        if claim is None:
            tiers.append(
                UNKNOWN
                + f" (PVC present, claim name not statically resolvable: {expr})"
            )
            continue
        full = f"{k8s_namespace}/{claim}"
        if full in r2_volumes:
            tiers.append("daily -> R2")
        elif full in weekly_volumes:
            tiers.append("weekly -> B2 (default target)")
        else:
            tiers.append("daily -> B2 (default group)")
    # Multiple PVCs on one role (e.g. pihole) can land in different tiers; report all,
    # de-duplicated, rather than picking one and hiding the rest.
    seen: list[str] = []
    for tier in tiers:
        if tier not in seen:
            seen.append(tier)
    return "; ".join(seen)


# Auto-deploy eligibility (k8s only — daniel-pi sets has_gitops: false)


def autodeploy_eligibility(
    entry: dict[str, Any],
    platform: str,
    host_data: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
) -> str:
    """Derive `entry`'s GitOps auto-deploy eligibility from its role's `k8s_autodeploy` default.

    Args:
        entry: The service's `containers_list` entry.
        platform: "k8s" or "docker" — only "k8s" has a GitOps auto-deploy path.
        host_data: The host's parsed `host_vars`, read for `has_gitops` off k8s.
        k8s_roles: Root directory of the k8s roles.

    Returns:
        "eligible", "denylisted (<reason>)", or an `UNKNOWN`/"n/a" explanation.
    """
    if platform != "k8s":
        if host_data.get("has_gitops") is False:
            return "n/a (host has no GitOps auto-deploy path)"
        return UNKNOWN + " (docker host's has_gitops not declared)"
    role_dir = k8s_roles / entry["name"]
    defaults = _load_yaml(role_dir / "defaults" / "main.yml")
    if "k8s_autodeploy" not in defaults:
        return UNKNOWN + " (role declares no k8s_autodeploy stance)"
    value = defaults["k8s_autodeploy"]
    if value is True:
        return "eligible"
    reason = defaults.get("k8s_autodeploy_reason")
    if isinstance(reason, str) and reason.strip():
        return f"denylisted ({reason.strip()})"
    return "denylisted (no reason given)"
