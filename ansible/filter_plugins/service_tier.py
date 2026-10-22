"""Ansible filter plugin deriving per-tier sets from the `tier` field on containers_list entries.

A service's tier is declared once, on its `containers_list` entry, and every set that ranks
services is derived from it (#3389). Four consumers read it:

- `tier_backup_claims`: the k3s role's `k3s_longhorn_r2_volumes`, the volumes backed up daily
  to R2, is every `backup_claims` volume of a `home-critical` entry.
- `tier_priority_class`: a tiered role's templates pass `containers_list |
  tier_priority_class('<role>')` to `pod_shell(...)` instead of a literal PriorityClass (#3452).
- `in_service_tier`: uptime-kuma's static monitors page a home-critical service's own tile by
  email and tag it `severity: critical`, so the email tier follows the tier field (#3480).
- `shed_entries`: `probe.py shed-set` prints the untiered workloads to scale down when one node
  is lost, so the tiered ones fit on the survivor (#3481). No template reads it, so it is not a
  registered filter. It drops `NEVER_SHED`, the untiered cluster plumbing no break-glass may
  scale to zero.

No Ansible import, so `scripts/lib/service_tiers.py` and the docs generators read the same
function the playbook runs. `FilterModule` re-raises a function's `ValueError` as
`AnsibleFilterError` prefixed with the filter's name.
"""

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

# The tiers an entry's `tier` may name, each with the PriorityClass its pods run at. One tier
# maps to exactly one class, which is why the house's services split in two: the edge and SSO
# run at homelab-critical, the automation chain one band below it. `platform` is critical
# without being home-critical (the registry, the UPS primary). A misspelt tier would silently
# drop the entry out of every derived set, so an unknown value raises rather than reading as
# untiered.
SERVICE_TIERS = {
    "home-edge": "homelab-critical",
    "home-automation": "homelab-automation",
    "platform": "homelab-critical",
}

# Names that select several tiers at once. `home-critical` is what the house and SSO stop
# working without, and is the set the daily R2 backup tier derives from.
TIER_GROUPS = {"home-critical": ("home-edge", "home-automation")}


# The untiered plumbing tags the lost-node break-glass must never scale down: H1's Flux-born
# roles whose `defaults/main.yml` sets `flux_prune: false`. CoreDNS at zero replicas stops
# cluster DNS, and Flux then cannot fetch the bundle that restores it. A tier is no substitute,
# because it derives a PriorityClass these roles do not run at. Each tag joins in the PR that
# adds its entry, and `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py` holds the set
# equal to the untiered `flux_prune: false` roles, so a plumbing tag cannot land without it.
# The full reasoning is the H1 plan's *Six Flux-born tags*.
NEVER_SHED = frozenset({"cluster-base", "coredns", "metallb"})


def _known():
    return ", ".join([*SERVICE_TIERS, *TIER_GROUPS])


def _declared_tier(entry):
    """The entry's `tier`, or None when it declares none. Raises ValueError on an unknown one."""
    declared = entry.get("tier")
    if declared is not None and declared not in SERVICE_TIERS:
        raise ValueError(
            f"containers_list entry {entry['name']!r} declares unknown tier {declared!r}; "
            f"known: {', '.join(SERVICE_TIERS)}"
        )
    return declared


def tier_entries(containers_list, tier):
    """The named entries of `containers_list` whose `tier` is in `tier`, in list order.

    `tier` is one of `SERVICE_TIERS` or a group name from `TIER_GROUPS`.

    Raises ValueError when `tier`, or any entry's `tier`, is unknown.
    """
    if tier in TIER_GROUPS:
        wanted = TIER_GROUPS[tier]
    elif tier in SERVICE_TIERS:
        wanted = (tier,)
    else:
        raise ValueError(f"unknown service tier {tier!r}; known: {_known()}")
    found = []
    for entry in containers_list or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if _declared_tier(entry) in wanted:
            found.append(entry)
    return found


def shed_entries(containers_list, never_shed=NEVER_SHED):
    """The named entries of `containers_list` that are in no tier, in list order.

    These are the workloads to scale down when one node is lost, so that every tier in
    `SERVICE_TIERS` fits on the survivor. The set is the complement of the tiers rather than
    a test for a missing key, so a tier added to `SERVICE_TIERS` leaves it on its own. An
    entry named in `never_shed` is dropped as well. Every caller but a test takes the default,
    `NEVER_SHED`, so `probe.py shed-set` and every Python reader of the set read one answer.

    Raises ValueError when any entry's `tier` is unknown: a misspelt tier must not put a
    critical service on the list of things to switch off.
    """
    kept = {
        entry["name"]
        for tier in SERVICE_TIERS
        for entry in tier_entries(containers_list, tier)
    }
    return [
        entry
        for entry in containers_list or []
        if isinstance(entry, dict)
        and entry.get("name")
        and entry["name"] not in kept
        and entry["name"] not in never_shed
    ]


def tier_backup_claims(containers_list, tier, namespace):
    """`namespace/claim` for every `backup_claims` volume of the entries in `tier`.

    `namespace` is the default for an entry that declares none of its own; pass
    `k8s_namespace`. The order is `containers_list` order, then each entry's own.
    """
    return [
        f"{entry.get('namespace') or namespace}/{claim}"
        for entry in tier_entries(containers_list, tier)
        for claim in entry.get("backup_claims") or []
    ]


def in_service_tier(containers_list, name, tier):
    """Whether the `containers_list` entry `name` is in `tier`.

    `tier` is one of `SERVICE_TIERS` or a group name from `TIER_GROUPS`.

    Raises ValueError when no entry is named `name`: a renamed or removed entry must fail the
    render rather than read as untiered, which would quietly drop its tile off the email tier.
    """
    if not any(
        isinstance(entry, dict) and entry.get("name") == name
        for entry in containers_list or []
    ):
        raise ValueError(f"no containers_list entry named {name!r}")
    return any(entry["name"] == name for entry in tier_entries(containers_list, tier))


def tier_priority_class(containers_list, name):
    """The PriorityClass the `containers_list` entry `name` runs at, from its `tier`.

    Raises ValueError when no entry is named `name`, or when it declares no tier: a template
    that asks has no literal to fall back on, and a pod with no class sits at priority 0.
    """
    for entry in containers_list or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            declared = _declared_tier(entry)
            if declared is None:
                raise ValueError(
                    f"containers_list entry {name!r} declares no tier, so it has no "
                    "derived priority class; add `tier:` or pass pod_shell a literal"
                )
            return SERVICE_TIERS[declared]
    raise ValueError(f"no containers_list entry named {name!r}")


class FilterModule:
    def filters(self):
        return {
            "in_service_tier": _as_filter(in_service_tier),
            "tier_backup_claims": _as_filter(tier_backup_claims),
            "tier_priority_class": _as_filter(tier_priority_class),
        }
