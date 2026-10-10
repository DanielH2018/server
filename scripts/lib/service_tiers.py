"""The sets derived from containers_list ``tier``, for readers outside Ansible.

``k3s_longhorn_r2_volumes`` is a Jinja expression over ``containers_list`` (#3389), and so are
``k3s_longhorn_weekly_volumes`` and ``k3s_longhorn_nobackup_volumes`` (#4207). A raw YAML read
of the k3s defaults returns the expressions rather than the volumes. Every Python reader of
the tier lists resolves them here, through the same filters
``ansible/filter_plugins/service_tier.py`` and ``longhorn_groups.py`` the playbook runs. ``shed_set`` is the lost-node
shed set ``probe.py shed-set`` prints, from the same module.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(
    0, str(_Path(__file__).resolve().parents[2] / "ansible" / "filter_plugins")
)

from lib.k8s_roles import k8s_entries
from lib.render_guard import load_yaml
from lib.repo_paths import ALL_VARS
from longhorn_groups import longhorn_nobackup_claims, longhorn_weekly_claims
from service_tier import shed_entries, tier_backup_claims

R2_TIER = "home-critical"  # a TIER_GROUPS name: home-edge and home-automation


def r2_volumes(
    entries: list[dict] | None = None, namespace: str | None = None
) -> list[str]:
    """``k3s_longhorn_r2_volumes`` as the k3s role computes it.

    Args:
        entries: the ``containers_list`` entries to derive from; daniel-box's k8s entries
            when omitted.
        namespace: the namespace for an entry that names none; ``k8s_namespace`` from
            ``group_vars/all.yml`` when omitted.
    """
    if entries is None:
        entries = list(k8s_entries().values())
    if namespace is None:
        namespace = load_yaml(ALL_VARS)["k8s_namespace"]
    return tier_backup_claims(entries, R2_TIER, namespace)


def shed_set(entries: list[dict] | None = None) -> list[str]:
    """The k8s services to scale down when one node is lost, in ``containers_list`` order.

    Args:
        entries: the ``containers_list`` entries to derive from; daniel-box's k8s entries
            when omitted. A Pi entry never runs on a k3s node, so the default leaves it out.
    """
    if entries is None:
        entries = list(k8s_entries().values())
    return [entry["name"] for entry in shed_entries(entries)]


# Each tier list the k3s role derives, and the filter that derives it from
# ``(entries, namespace)``.
_DERIVED = {
    "k3s_longhorn_r2_volumes": lambda entries, ns: tier_backup_claims(
        entries, R2_TIER, ns
    ),
    "k3s_longhorn_weekly_volumes": longhorn_weekly_claims,
    "k3s_longhorn_nobackup_volumes": longhorn_nobackup_claims,
}


def resolved_tier_lists(k3s_defaults: dict, entries: list[dict] | None = None) -> dict:
    """A copy of the k3s role's raw defaults with each derived tier list resolved.

    The derived lists are ``k3s_longhorn_r2_volumes``, ``k3s_longhorn_weekly_volumes`` (a
    ``{namespace/pvcName: shard}`` map) and ``k3s_longhorn_nobackup_volumes``. A value that is
    already a list or a map, as a test fixture writes it, is kept as written; only the role's
    Jinja expressions are replaced by the derived volumes.

    Args:
        k3s_defaults: The role's defaults, already layered under the inventory when the
            caller has one. Its ``k8s_namespace``, when present, is the namespace the
            expressions derive with.
        entries: The ``containers_list`` entries the expressions derive from; daniel-box's
            k8s entries in the repo inventory when omitted. A caller reading an injected
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
        namespace = load_yaml(ALL_VARS)["k8s_namespace"]
    for key in pending:
        resolved[key] = _DERIVED[key](entries, namespace)
    return resolved
