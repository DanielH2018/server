"""The sets derived from containers_list ``tier``, for readers outside Ansible.

``k3s_longhorn_r2_volumes`` is a Jinja expression over ``containers_list`` (#3389), so a raw
YAML read of the k3s defaults returns the expression rather than the volumes. Every Python
reader of the tier lists resolves them here, through the same filter
``ansible/filter_plugins/service_tier.py`` the playbook runs. ``shed_set`` is the lost-node
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


def resolved_tier_lists(k3s_defaults: dict) -> dict:
    """A copy of the k3s role's raw defaults with ``k3s_longhorn_r2_volumes`` resolved.

    A value that is already a list, as a test fixture writes it, is kept as written; only the
    role's Jinja expression is replaced by the derived volumes.
    """
    if isinstance(k3s_defaults.get("k3s_longhorn_r2_volumes"), list):
        return dict(k3s_defaults)
    return {**k3s_defaults, "k3s_longhorn_r2_volumes": r2_volumes()}
