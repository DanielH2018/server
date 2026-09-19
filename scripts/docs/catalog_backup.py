#!/usr/bin/env python3
"""Longhorn backup tier and GitOps auto-deploy eligibility for one ``containers_list`` entry.

Split out of ``scripts/docs/service_catalog.py`` on 2026-09-04. Both facts are read from a
k8s role's own files — its PVC claims and their StorageClass for the tier, its
``k8s_autodeploy`` default for eligibility — and both report a reason rather than a guess
when the role does not say. The FIELD NOTES in the generator's own docstring record which
cases those are and why.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from catalog_model import K3S_DEFAULTS, K8S_ROLES, UNKNOWN
from lib import yaml_fast
from lib.render_guard import load_yaml as _load_yaml

__all__ = [
    "ClaimDecl",
    "LonghornTiers",
    "autodeploy_eligibility",
    "autodeploy_stance",
    "backup_tier",
    "claim_index",
    "claim_names",
    "load_longhorn_tier_lists",
]


# Backup tier (k8s / Longhorn only — Pi's Docker volumes are not Longhorn-backed)
#
# A k8s role declares a PVC in one of three shapes, and each names the claim AND its
# StorageClass — the class is what decides whether Longhorn backs the volume up at all
# (`longhorn` asks for backups, `longhorn-nobackup` does not; see the comment above "Find PVCs
# whose StorageClass asks for backups" in ansible/roles/setup/k3s/tasks/longhorn.yml):
#
#   1. an inline `kind: PersistentVolumeClaim` block carrying its own `storageClassName:` line
#      (media-volume, valheim, zigbee2mqtt, freshrss, code-server's workspace);
#   2. a call to the shared `pvc()` macro in ansible/templates/pvc.yml.j2, whose second
#      positional argument is the class (authelia, registry, karakeep-meili, claude-otel's four);
#   3. an `include_role: k8s/volume-claim` task, whose `vars:` carry `volume_claim_name` and,
#      optionally, `volume_claim_storage_class` — the volume-claim role's own default applies
#      when the caller leaves it out (uptime-kuma, karakeep's main claim, the *arrs, ~20 roles).
#      Nothing about such a claim appears in the calling role's templates/ at all.
#
# A `claimName:` reference in a pod spec is the fourth pattern. It declares nothing — it names
# a claim one of the three shapes above declares, in this role or another (`media-data` is
# media-volume's, mounted by seven consumers), which is why `claim_index` is built across
# every role before any one role is classified.

# Shape 1. The body runs to the next document or object so `storageClassName:` is read from
# THIS claim, not the PersistentVolume that follows it in media-volume's template.
_PVC_BLOCK_RE = re.compile(
    r"kind:\s*PersistentVolumeClaim.*?metadata:\s*\n\s*name:\s*(?P<name>\{\{.*?\}\}|\S+)"
    r"(?P<body>.*?)(?=\n---|\nkind:|\Z)",
    re.DOTALL,
)
_STORAGE_CLASS_RE = re.compile(r"storageClassName:\s*(\{\{.*?\}\}|\S+)")
# Shape 2. Only the first two positional arguments are read: `pvc(name, storage_class, ...)`.
# An argument is a quoted literal or a bare variable name — the macro's signature admits
# nothing else at these positions in any current caller.
_MACRO_ARG = r"'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*"
_PVC_MACRO_CALL_RE = re.compile(
    rf"\bpvc\(\s*(?P<name>{_MACRO_ARG})\s*,\s*(?P<storage_class>{_MACRO_ARG})\s*,"
)
# Shape 3 is read from parsed YAML, not a regex: see _volume_claim_includes.
_VOLUME_CLAIM_ROLE = "k8s/volume-claim"
# The fourth pattern — a reference, not a declaration.
_CLAIM_NAME_RE = re.compile(r"claimName:\s*(\{\{.*?\}\}|\S+)")
_SIMPLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


@dataclass(frozen=True)
class ClaimDecl:
    """One PVC a k8s role declares, resolved as far as the role's own defaults allow.

    Attributes:
        name: The claim name — a literal when resolvable, else the expression as written.
        storage_class: The StorageClass literal, or None when it is not statically resolvable.
        resolved: Whether `name` is a literal (False leaves `name` as the raw expression).
    """

    name: str
    storage_class: str | None
    resolved: bool


@dataclass(frozen=True)
class LonghornTiers:
    """The three "namespace/claim" lists the k3s role's defaults sort Longhorn volumes into.

    A volume in none of them and on a backup-asking StorageClass falls into the `default`
    RecurringJob group (daily, to B2) — see
    ansible/roles/setup/k3s/templates/longhorn-recurringjob.yaml.j2.
    """

    r2: frozenset[str] = field(default_factory=frozenset)
    weekly: frozenset[str] = field(default_factory=frozenset)
    nobackup: frozenset[str] = field(default_factory=frozenset)


def _macro_arg_expr(arg: str) -> str:
    """A `pvc()` macro argument as the template expression the resolver reads.

    `'literal'` becomes `literal`; a bare variable name becomes `{{ name }}`, the form a
    PVC block's `name:` line already uses, so one resolver serves both shapes.
    """
    if arg[0] in "'\"":
        return arg[1:-1]
    return "{{ " + arg + " }}"


def _resolve_expr(expr: str, role_dir: Path) -> str | None:
    """Resolve a template expression (claim name or StorageClass) to a literal string.

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


def _decl(name_expr: str, class_expr: str | None, role_dir: Path) -> ClaimDecl:
    name = _resolve_expr(name_expr, role_dir)
    storage_class = (
        _resolve_expr(class_expr, role_dir) if class_expr is not None else None
    )
    return ClaimDecl(
        name=name if name is not None else name_expr,
        storage_class=storage_class,
        resolved=name is not None,
    )


def _template_declarations(role_dir: Path) -> list[ClaimDecl]:
    """Shapes 1 and 2: every PVC a role's templates/*.j2 declare, with its StorageClass."""
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    decls = []
    for tmpl in sorted(templates.glob("*.j2")):
        text = tmpl.read_text()
        for block in _PVC_BLOCK_RE.finditer(text):
            sc = _STORAGE_CLASS_RE.search(block.group("body"))
            decls.append(
                _decl(block.group("name"), sc.group(1) if sc else None, role_dir)
            )
        for call in _PVC_MACRO_CALL_RE.finditer(text):
            decls.append(
                _decl(
                    _macro_arg_expr(call.group("name")),
                    _macro_arg_expr(call.group("storage_class")),
                    role_dir,
                )
            )
    return decls


def _volume_claim_includes(role_dir: Path, k8s_roles: Path) -> list[ClaimDecl]:
    """Shape 3: every PVC a role has the volume-claim role render on its behalf.

    Read from the `include_role` task's `vars:` in tasks/*.yml. A caller that sets no
    `volume_claim_storage_class` gets the volume-claim role's own default, read from that
    role's defaults rather than assumed — absent both, the class is unknown. Best-effort in
    the same way as `lib.k8s_pvc.volume_claim_pvc_names`: only a plain string `vars:` value is
    read, which is the only form any current caller uses.
    """
    tasks_dir = role_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    vc_defaults = _load_yaml(k8s_roles / "volume-claim" / "defaults" / "main.yml")
    default_class = vc_defaults.get("volume_claim_storage_class")
    decls = []
    for task_file in sorted(tasks_dir.glob("*.yml")):
        try:
            tasks = yaml_fast.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            inc = task.get("ansible.builtin.include_role")
            if not isinstance(inc, dict) or inc.get("name") != _VOLUME_CLAIM_ROLE:
                continue
            task_vars = task.get("vars") or {}
            name = task_vars.get("volume_claim_name")
            if not isinstance(name, str):
                continue
            storage_class = task_vars.get("volume_claim_storage_class", default_class)
            decls.append(
                _decl(
                    name,
                    storage_class if isinstance(storage_class, str) else None,
                    role_dir,
                )
            )
    return decls


def _declared_claims(role_dir: Path, k8s_roles: Path) -> list[ClaimDecl]:
    return _template_declarations(role_dir) + _volume_claim_includes(
        role_dir, k8s_roles
    )


def _referenced_claim_exprs(role_dir: Path) -> list[str]:
    """Every `claimName:` expression in a role's templates — references, not declarations."""
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    exprs = []
    for tmpl in sorted(templates.glob("*.j2")):
        exprs.extend(_CLAIM_NAME_RE.findall(tmpl.read_text()))
    return exprs


def _role_claims(role_dir: Path, k8s_roles: Path) -> list[ClaimDecl]:
    """Every claim a role references or declares, de-duplicated by name.

    References come first, so the order is the role's own mount order — the order its
    `CLAUDE.md` At-a-glance block already prints — and a declaration the pod never mounts
    (pihole's two, provisioned for a templated Deployment) follows. A name in both takes the
    declaration's StorageClass; a reference alone is left None here and looked up in
    `claim_index` by the classifier, since the declaring role may be another one.
    """
    declared = _declared_claims(role_dir, k8s_roles)
    by_name = {claim.name: claim for claim in declared if claim.resolved}
    ordered = [
        _decl(expr, None, role_dir) for expr in _referenced_claim_exprs(role_dir)
    ]
    ordered.extend(declared)
    seen: list[ClaimDecl] = []
    for claim in ordered:
        if claim.name not in {c.name for c in seen}:
            seen.append(by_name.get(claim.name, claim))
    return seen


def claim_index(k8s_roles: Path = K8S_ROLES) -> dict[str, str | None]:
    """Every resolvable claim name declared under `k8s_roles`, mapped to its StorageClass.

    Built across every role directory — including ones with no `containers_list` entry — so
    a claim mounted in one role and declared in another (`media-data`) resolves. A claim
    declared with a class the declaring role's defaults cannot name maps to None.
    """
    index: dict[str, str | None] = {}
    if not k8s_roles.is_dir():
        return index
    for role_dir in sorted(p for p in k8s_roles.iterdir() if p.is_dir()):
        for claim in _declared_claims(role_dir, k8s_roles):
            if claim.resolved and index.get(claim.name) is None:
                index[claim.name] = claim.storage_class
    return index


def claim_names(role_dir: Path, k8s_roles: Path = K8S_ROLES) -> list[str]:
    """Every PVC claim a k8s role declares or references, resolved where its defaults allow.

    An expression the role's own defaults cannot resolve is returned as written (the catalogue
    reports those as unknown); `gen_role_glance.py` lists the names, and `backup_tier` below
    classifies them.
    """
    return [claim.name for claim in _role_claims(role_dir, k8s_roles)]


def load_longhorn_tier_lists(k3s_defaults: Path = K3S_DEFAULTS) -> LonghornTiers:
    """The R2, weekly and no-backup volume lists from the k3s role's defaults."""
    data = _load_yaml(k3s_defaults)
    return LonghornTiers(
        r2=frozenset(data.get("k3s_longhorn_r2_volumes") or []),
        weekly=frozenset(data.get("k3s_longhorn_weekly_volumes") or []),
        nobackup=frozenset(data.get("k3s_longhorn_nobackup_volumes") or []),
    )


def _is_longhorn(storage_class: str) -> bool:
    return storage_class.startswith("longhorn")


def _classify(claim: ClaimDecl, full: str, tiers: LonghornTiers) -> str:
    """One claim's tier, from its StorageClass and the three lists.

    The order is the one longhorn.yml's reconciliation applies: a class outside Longhorn's
    is never backed up by it; `longhorn-nobackup` and the no-backup list both exclude a
    volume (the list overrides the class for volumes bound before the distinction existed);
    the R2 and weekly lists pick a target for what remains, and the `default` RecurringJob
    group takes the rest. A claim in one of the lists is classified by the list even when
    its class is unknown — the lists are Longhorn's own selectors — but a claim in none of
    them with no resolvable class is reported unknown, not assumed backed up.
    """
    sc = claim.storage_class
    if sc is not None and not _is_longhorn(sc):
        return f"not Longhorn ({sc})"
    if sc == "longhorn-nobackup":
        return "no backup (StorageClass longhorn-nobackup)"
    if full in tiers.nobackup:
        return "no backup (listed in k3s_longhorn_nobackup_volumes)"
    if full in tiers.r2:
        return "daily -> R2"
    if full in tiers.weekly:
        return "weekly -> B2 (default target)"
    if sc is None:
        return (
            UNKNOWN + f" (claim {claim.name}: StorageClass not statically resolvable)"
        )
    return "daily -> B2 (default group)"


def backup_tier(
    entry: dict[str, Any],
    platform: str,
    k8s_namespace: str,
    tiers: LonghornTiers,
    k8s_roles: Path = K8S_ROLES,
    claim_classes: dict[str, str | None] | None = None,
) -> str:
    """Derive `entry`'s Longhorn backup tier(s) from its role's PVC claims.

    Resolves each PVC the role declares (or references by `claimName:`) to a literal claim
    name and a StorageClass — its own declaration first, then `claim_classes` for a claim
    another role declares — and classifies `namespace/claim` as `_classify` describes. A
    role with multiple PVCs in different tiers reports all of them, de-duplicated. Every
    claim is classified under `k8s_namespace`: the one role whose claims live elsewhere
    (claude-otel, in the observability namespace) is on `longhorn-nobackup` by class, so
    the namespace never reaches a list lookup for it.

    Args:
        entry: The service's `containers_list` entry.
        platform: "k8s" or "docker" — only "k8s" is Longhorn-backed.
        k8s_namespace: The cluster namespace PVCs are classified under.
        tiers: The R2, weekly and no-backup "namespace/claim" lists.
        k8s_roles: Root directory of the k8s roles.
        claim_classes: `claim_index(k8s_roles)`, built once by the caller; built here if
            not given.

    Returns:
        A semicolon-joined string of tier labels, "no PVC (stateless)", or "n/a" off k8s.
    """
    if platform != "k8s":
        return "n/a (Docker/Pi, not Longhorn-backed)"
    role_dir = k8s_roles / entry["name"]
    claims = _role_claims(role_dir, k8s_roles)
    if not claims:
        return "no PVC (stateless)"
    if claim_classes is None:
        claim_classes = claim_index(k8s_roles)
    tiers_out = []
    for claim in claims:
        if not claim.resolved:
            tiers_out.append(
                UNKNOWN
                + f" (PVC present, claim name not statically resolvable: {claim.name})"
            )
            continue
        if claim.storage_class is None:
            claim = ClaimDecl(claim.name, claim_classes.get(claim.name), True)
        tiers_out.append(_classify(claim, f"{k8s_namespace}/{claim.name}", tiers))
    # Multiple PVCs on one role (e.g. pihole) can land in different tiers; report all,
    # de-duplicated, rather than picking one and hiding the rest.
    seen: list[str] = []
    for tier in tiers_out:
        if tier not in seen:
            seen.append(tier)
    return "; ".join(seen)


# Auto-deploy eligibility (k8s only — daniel-pi sets has_gitops: false)


def autodeploy_stance(role_dir: Path) -> tuple[bool | None, str]:
    """A k8s role's `k8s_autodeploy` declaration as `(stance, reason)`.

    `stance` is True (eligible), False (denylisted) or None (undeclared). `reason` is the
    role's own `k8s_autodeploy_reason` for a denylisted role, stripped, and "" otherwise.
    """
    defaults = _load_yaml(role_dir / "defaults" / "main.yml")
    if "k8s_autodeploy" not in defaults:
        return None, ""
    if defaults["k8s_autodeploy"] is True:
        return True, ""
    reason = defaults.get("k8s_autodeploy_reason")
    return False, reason.strip() if isinstance(reason, str) else ""


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
    stance, reason = autodeploy_stance(k8s_roles / entry["name"])
    if stance is None:
        return UNKNOWN + " (role declares no k8s_autodeploy stance)"
    if stance:
        return "eligible"
    return f"denylisted ({reason or 'no reason given'})"
