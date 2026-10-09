#!/usr/bin/env python3
"""Longhorn backup tier and GitOps auto-deploy eligibility for one ``containers_list`` entry.

Both facts are read from a k8s role's own files — its PVC claims and their StorageClass for
the tier, its ``k8s_autodeploy`` default for eligibility — and both report a reason rather
than a guess when the role does not say. The FIELD NOTES in the generator's own docstring
record which cases those are and why.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from catalog_model import K3S_DEFAULTS, K8S_ROLES, UNKNOWN
from lib.jinja_comments import strip_jinja_comments
from lib.k8s_roles import role_dirs
from lib.estate import Estate, role_defaults
from lib.render_guard import load_yaml as _load_yaml
from lib.repo_paths import SHARED_TPL
from lib.service_tiers import resolved_tier_lists

__all__ = [
    "ClaimDecl",
    "LonghornTiers",
    "autodeploy_eligibility",
    "autodeploy_stance",
    "backup_tier",
    "claim_index",
    "claim_names",
    "claim_tiers",
    "load_longhorn_tier_lists",
]


# Backup tier (k8s / Longhorn only — Pi's Docker volumes are not Longhorn-backed)
#
# A k8s role declares a PVC in one of two shapes, and each names the claim AND its
# StorageClass — the class is what decides whether Longhorn backs the volume up at all
# (`longhorn` asks for backups, `longhorn-nobackup` does not; see the comment above "Find PVCs
# whose StorageClass asks for backups" in ansible/roles/setup/k3s/tasks/longhorn.yml):
#
#   1. an inline `kind: PersistentVolumeClaim` block carrying its own `storageClassName:` line
#      (no role's template carries one since media-volume moved to `k8s_claims`; the shape stays
#      read so a new one is classified);
#   2. a `k8s_claims` entry in the role's defaults/main.yml, `{name, size, storage_class}`,
#      which k8s/manifests renders from the shared claim-default.yaml.j2 (every other claim).
#
# A `claimName:` reference in a pod spec is the third pattern. It declares nothing — it names
# a claim one of the two shapes above declares, in this role or another (`media-data` is
# media-volume's, mounted by seven consumers), which is why `claim_index` is built across
# every role before any one role is classified.

# Shape 1. The body runs to the next document or object so `storageClassName:` is read from
# THIS claim, not a PersistentVolume that follows it in the same template.
_PVC_BLOCK_RE = re.compile(
    r"kind:\s*PersistentVolumeClaim.*?metadata:\s*\n\s*name:\s*(?P<name>\{\{.*?\}\}|\S+)"
    r"(?P<body>.*?)(?=\n---|\nkind:|\Z)",
    re.DOTALL,
)
_STORAGE_CLASS_RE = re.compile(r"storageClassName:\s*(\{\{.*?\}\}|\S+)")
# A shared macro's argument: a quoted literal or a bare variable name.
_MACRO_ARG = r"'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*"
# The third pattern — a reference, not a declaration.
_CLAIM_NAME_RE = re.compile(r"claimName:\s*(\{\{.*?\}\}|\S+)")
_SIMPLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
# The fourth: a `claimName:` inside a SHARED macro the role's template calls, naming a macro
# parameter the call site binds (`arr-deployment.yml.j2`, radarr's and sonarr's whole
# Deployment). The claim is still the role's — it is mounted by its pod — but no line of the
# role's own templates names it, so a scan that stops at `templates/` drops it silently. radarr
# and sonarr lost `media-data` from their At-a-glance blocks exactly that way.
_SHARED_IMPORT_RE = re.compile(
    r"\{%-?\s*from\s*'(?P<file>[^']+)'\s*import\s+(?P<names>[^%]*?)\s*(?:with context\s*)?-?%\}"
)
_KWARG_RE = re.compile(
    rf"(?P<param>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>{_MACRO_ARG})"
)


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
    """A shared macro's argument as the template expression the resolver reads.

    `'literal'` becomes `literal`; a bare variable name becomes `{{ name }}`, the form a
    PVC block's `name:` line already uses, so one resolver serves both.
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
    value = _ESTATE.role_vars(role_dir).get(var)
    if isinstance(value, str) and "{{" not in value:
        return value
    return None


# One parse of the inventory per run: every claim of every role resolves through it.
_ESTATE = Estate()


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
    """Shape 1: every PVC a role's templates/*.j2 declare, with its StorageClass."""
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    decls = []
    for tmpl in sorted(templates.glob("*.j2")):
        text = strip_jinja_comments(tmpl.read_text())
        for block in _PVC_BLOCK_RE.finditer(text):
            sc = _STORAGE_CLASS_RE.search(block.group("body"))
            decls.append(
                _decl(block.group("name"), sc.group(1) if sc else None, role_dir)
            )
    return decls


def _k8s_claims_entries(role_dir: Path) -> list[ClaimDecl]:
    """Shape 2: every `k8s_claims` entry in the role's defaults, name and class as written."""
    claims = role_defaults(role_dir).get("k8s_claims") or []
    return [
        _decl(
            claim["name"],
            claim.get("storage_class")
            if isinstance(claim.get("storage_class"), str)
            else None,
            role_dir,
        )
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("name"), str)
    ]


def _declared_claims(role_dir: Path) -> list[ClaimDecl]:
    return _template_declarations(role_dir) + _k8s_claims_entries(role_dir)


def _call_kwargs(text: str, macro: str) -> dict[str, str]:
    """The keyword arguments of the first `macro(...)` call in `text`, as written.

    Reads to the matching close paren rather than to the first one, so a call carrying a
    nested call (`port=container_item.port`) is not cut short. Positional arguments are
    ignored: a claim is always passed by name at these call sites.
    """
    start = text.find(macro + "(")
    if start < 0:
        return {}
    depth, i = 0, start + len(macro)
    for i in range(start + len(macro), len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
    return {
        m.group("param"): m.group("value") for m in _KWARG_RE.finditer(text[start:i])
    }


def _shared_macro_claim_exprs(text: str, shared_tpl: Path) -> list[str]:
    """Shape 5: every claim a shared macro this template calls mounts on its behalf.

    A `claimName: {{ <param> }}` in the macro body is rewritten to the expression the call
    site binds that parameter to, so the role's own defaults resolve it as they would a
    `claimName:` written out here. A macro-body claim naming anything but a bare parameter
    is passed through unchanged — it resolves or reports unknown on its own terms.
    """
    exprs = []
    for imp in _SHARED_IMPORT_RE.finditer(text):
        macro_file = shared_tpl / imp.group("file")
        if not macro_file.is_file():
            continue
        body = strip_jinja_comments(macro_file.read_text())
        for name in (n.strip() for n in imp.group("names").split(",")):
            if not name or name + "(" not in text:
                continue
            bound = _call_kwargs(text, name)
            for expr in _CLAIM_NAME_RE.findall(body):
                param = _SIMPLE_VAR_RE.match(expr)
                if param and param.group(1) in bound:
                    exprs.append(_macro_arg_expr(bound[param.group(1)]))
                else:
                    exprs.append(expr)
    return exprs


def _referenced_claim_exprs(role_dir: Path, shared_tpl: Path = SHARED_TPL) -> list[str]:
    """Every `claimName:` expression a role's templates name, their own or a shared macro's."""
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    exprs = []
    for tmpl in sorted(templates.glob("*.j2")):
        text = strip_jinja_comments(tmpl.read_text())
        exprs.extend(_CLAIM_NAME_RE.findall(text))
        exprs.extend(_shared_macro_claim_exprs(text, shared_tpl))
    return exprs


def _role_claims(role_dir: Path, k8s_roles: Path) -> list[ClaimDecl]:
    """Every claim a role references or declares, de-duplicated by name.

    References come first, so the order is the role's own mount order — the order its
    `CLAUDE.md` At-a-glance block already prints — and a declaration the pod never mounts
    (pihole's two, provisioned for a templated Deployment) follows. A name in both takes the
    declaration's StorageClass; a reference alone is left None here and looked up in
    `claim_index` by the classifier, since the declaring role may be another one.
    """
    declared = _declared_claims(role_dir)
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
    for role_dir in role_dirs(k8s_roles):
        for claim in _declared_claims(role_dir):
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
    data = resolved_tier_lists(_load_yaml(k3s_defaults))
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


def claim_tiers(
    role_dir: Path,
    *,
    k8s_namespace: str,
    tiers: LonghornTiers,
    k8s_roles: Path = K8S_ROLES,
    claim_classes: dict[str, str | None] | None = None,
) -> list[tuple[str, str]]:
    """`(claim, tier)` for every PVC a k8s role declares or references, in mount order.

    Resolves each claim to a literal name and a StorageClass — its own declaration first,
    then `claim_classes` for a claim another role declares — and classifies
    `namespace/claim` as `_classify` describes. A claim whose name the role's own defaults
    cannot resolve is returned as the expression written, with an "unknown" tier saying so.
    Every claim is classified under `k8s_namespace`: the one role whose claims live elsewhere
    (observability, in the observability namespace) is on `longhorn-nobackup` by class, so the
    namespace never reaches a list lookup for it.

    This is the one derivation both readers print — `backup_tier` joins it into the
    catalogue's column, `gen_role_glance.py` prints it beside each claim — so the two
    cannot disagree.

    Args:
        role_dir: The k8s role directory.
        k8s_namespace: The cluster namespace PVCs are classified under.
        tiers: The R2, weekly and no-backup "namespace/claim" lists.
        k8s_roles: Root directory of the k8s roles.
        claim_classes: `claim_index(k8s_roles)`, built once by the caller; built here if
            not given.
    """
    claims = _role_claims(role_dir, k8s_roles)
    if not claims:
        return []
    if claim_classes is None:
        claim_classes = claim_index(k8s_roles)
    out: list[tuple[str, str]] = []
    for claim in claims:
        if not claim.resolved:
            out.append(
                (
                    claim.name,
                    UNKNOWN
                    + f" (PVC present, claim name not statically resolvable: {claim.name})",
                )
            )
            continue
        if claim.storage_class is None:
            claim = ClaimDecl(claim.name, claim_classes.get(claim.name), True)
        out.append(
            (claim.name, _classify(claim, f"{k8s_namespace}/{claim.name}", tiers))
        )
    return out


def backup_tier(
    entry: dict[str, Any],
    platform: str,
    k8s_namespace: str,
    tiers: LonghornTiers,
    k8s_roles: Path = K8S_ROLES,
    claim_classes: dict[str, str | None] | None = None,
) -> str:
    """Derive `entry`'s Longhorn backup tier(s) from its role's PVC claims.

    The catalogue's one-cell form of `claim_tiers`: a role with multiple PVCs in different
    tiers reports all of them, de-duplicated.

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
    tiers_out = [
        tier
        for _, tier in claim_tiers(
            k8s_roles / entry["name"],
            k8s_namespace=k8s_namespace,
            tiers=tiers,
            k8s_roles=k8s_roles,
            claim_classes=claim_classes,
        )
    ]
    if not tiers_out:
        return "no PVC (stateless)"
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
    defaults = role_defaults(role_dir)
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
