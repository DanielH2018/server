"""Ansible filter plugin deriving per-tier sets from the `tier` field on containers_list entries.

A service's tier is declared once, on its `containers_list` entry, and every set that ranks
services is derived from it (#3389). Two consumers read it:

- `tier_backup_claims`: the k3s role's `k3s_longhorn_r2_volumes`, the volumes backed up daily
  to R2, is every `backup_claims` volume of a `home-critical` entry.
- `tier_priority_class`: a tiered role's templates pass `containers_list |
  tier_priority_class('<role>')` to `pod_shell(...)` instead of a literal PriorityClass (#3452).

No Ansible import, so `scripts/lib/service_tiers.py` and the docs generators read the same
function the playbook runs. Ansible wraps a filter's `ValueError` in its own error.
"""

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
            "tier_backup_claims": tier_backup_claims,
            "tier_priority_class": tier_priority_class,
        }
