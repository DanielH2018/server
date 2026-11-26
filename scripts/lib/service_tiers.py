"""The sets derived from each service entry's ``tier``, for readers outside Ansible.

``k3s_longhorn_r2_volumes`` is a Jinja expression over ``services``, and so are
``k3s_longhorn_weekly_volumes`` and ``k3s_longhorn_nobackup_volumes``. The three live
in the ``longhorn-reconcile`` role's defaults (``repo_paths.LONGHORN_RECONCILE_ROLE``) since
H1.7a. The schedule, retain and arm keys are the ``longhorn-backup`` role's since H1.7b
(``repo_paths.LONGHORN_BACKUP_ROLE``). A raw YAML read of those defaults returns the expressions
rather than the volumes. Every
Python reader of the tier lists resolves them here, through the same filters
``scripts/renderer/template_filters/service_tier.py`` and ``longhorn_groups.py`` the playbook runs. ``shed_set`` is the lost-node
shed set ``probe.py shed-set`` prints, from the same module.
"""

from lib.k8s_roles import k8s_entries
from lib.estate import shared_vars
from renderer.template_filters.longhorn_groups import (
    longhorn_nobackup_claims,
    longhorn_r2_claims,
    longhorn_weekly_claims,
)
from renderer.template_filters.service_tier import shed_entries


def r2_volumes(
    entries: list[dict] | None = None, namespace: str | None = None
) -> list[str]:
    """``k3s_longhorn_r2_volumes`` as the k3s role computes it.

    Args:
        entries: the service entries to derive from; ``k8s/services.yml``'s when omitted.
        namespace: the namespace for an entry that names none; ``k8s_namespace`` from
            ``k8s/vars.yml`` when omitted.
    """
    if entries is None:
        entries = list(k8s_entries().values())
    if namespace is None:
        namespace = shared_vars()["k8s_namespace"]
    return longhorn_r2_claims(entries, namespace)


def shed_set(entries: list[dict] | None = None) -> list[str]:
    """The k8s services to scale down when one node is lost, in ``k8s/services.yml`` order.

    Args:
        entries: the service entries to derive from; ``k8s/services.yml``'s when omitted.
    """
    if entries is None:
        entries = list(k8s_entries().values())
    return [entry["name"] for entry in shed_entries(entries)]


_DERIVED = {
    "k3s_longhorn_r2_volumes": longhorn_r2_claims,
    "k3s_longhorn_weekly_volumes": longhorn_weekly_claims,
    "k3s_longhorn_nobackup_volumes": longhorn_nobackup_claims,
}


def resolved_tier_lists(k3s_defaults: dict, entries: list[dict] | None = None) -> dict:
    """A copy of a role's raw defaults with each derived tier list resolved.

    The expressions sit in the ``longhorn-reconcile`` role's defaults. A dict that lacks a tier
    key is treated like one holding the expression, so a caller that passes the k3s role's
    defaults, to read its schedule keys as well, still gets all three lists.

    The derived lists are ``k3s_longhorn_r2_volumes``, ``k3s_longhorn_weekly_volumes`` (a
    ``{namespace/pvcName: shard}`` map) and ``k3s_longhorn_nobackup_volumes``. A value that is
    already a list or a map, as a test fixture writes it, is kept as written; only the role's
    Jinja expressions are replaced by the derived volumes.

    Args:
        k3s_defaults: The role's defaults, already layered under the inventory when the
            caller has one. Its ``k8s_namespace``, when present, is the namespace the
            expressions derive with.
        entries: The service entries the expressions derive from; the repo's
            ``k8s/services.yml`` entries when omitted. A caller reading an injected
            inventory passes that inventory's entries, or the derivation reads the repo's.
    """
    resolved = dict(k3s_defaults)
    pending = [k for k in _DERIVED if not isinstance(resolved.get(k), (list, dict))]
    if not pending:
        return resolved
    if entries is None:
        entries = list(k8s_entries().values())
    namespace = k3s_defaults.get("k8s_namespace")
    if namespace is None:
        namespace = shared_vars()["k8s_namespace"]
    for key in pending:
        resolved[key] = _DERIVED[key](entries, namespace)
    return resolved
