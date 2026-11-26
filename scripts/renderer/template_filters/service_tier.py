"""Filters deriving per-tier sets from the `tier` field on containers_list entries.

A service's tier is declared once, on its `containers_list` entry, and every set that ranks
services is derived from it. Four consumers read it:

- `tier_entries`: `longhorn_groups.longhorn_r2_claims` takes the volumes backed up daily to
  R2 from the `backup_claims` of the `home-critical` entries it returns.
- `tier_priority_class`: a tiered role's templates pass `containers_list |
  tier_priority_class('<role>')` to `pod_shell(...)` instead of a literal PriorityClass.
- `in_service_tier`: observability's `blackbox-ingress` and `blackbox-extra` probe jobs give a
  home-critical service's target `page_tier: critical`, so its page reaches email as well as
  Discord and the email tier follows the tier field.
- `shed_entries`: `probe.py shed-set` prints the untiered workloads to scale down when one node
  is lost, so the tiered ones fit on the survivor. No template reads it, so it is not a
  registered filter. It drops `NEVER_SHED`, the untiered cluster plumbing no break-glass may
  scale to zero.

`scripts/lib/service_tiers.py` and the docs generators read the same functions the
renderer runs. `lib.guard_jinja_env` registers each so a `ValueError` reaches the operator as a
`FilterError` prefixed with the filter's name.
"""

from lib.container_entry import SERVICE_TIERS

# `home-critical` is what the house and SSO stop working without, and the set the daily R2 backup
# tier derives from.
TIER_GROUPS = {"home-critical": ("home-edge", "home-automation")}


# Plumbing tags the lost-node break-glass must never scale down: CoreDNS at zero stops cluster DNS,
# so Flux cannot fetch the bundle that restores it. A test holds the set equal to the untiered
# `flux_prune: false` roles.
NEVER_SHED = frozenset(
    {"cluster-base", "coredns", "longhorn", "longhorn-backup", "metallb"}
)


def _known():
    return ", ".join([*SERVICE_TIERS, *TIER_GROUPS])


def tier_entries(containers_list, tier):
    """The entries of `containers_list` whose `tier` is in `tier`, in list order.

    `containers_list` is `parse_containers_list`'s result, so every entry's tier is known.
    `tier` is one of `SERVICE_TIERS` or a group name from `TIER_GROUPS`.

    Raises ValueError when `tier` is unknown.
    """
    if tier in TIER_GROUPS:
        wanted = TIER_GROUPS[tier]
    elif tier in SERVICE_TIERS:
        wanted = (tier,)
    else:
        raise ValueError(f"unknown service tier {tier!r}; known: {_known()}")
    return [entry for entry in containers_list or [] if entry.get("tier") in wanted]


def shed_entries(containers_list, never_shed=NEVER_SHED):
    """The entries of `containers_list` that are in no tier, in list order.

    These are the workloads to scale down when one node is lost, so that every tier in
    `SERVICE_TIERS` fits on the survivor. The set is the complement of the tiers rather than
    a test for a missing key, so a tier added to `SERVICE_TIERS` leaves it on its own. An
    entry named in `never_shed` is dropped as well. Every caller but a test takes the default,
    `NEVER_SHED`, so `probe.py shed-set` and every Python reader of the set read one answer.
    A misspelt tier cannot put a critical service on the list of things to switch off,
    because `parse_containers_list` refuses it before any reader runs.
    """
    return [
        entry
        for entry in containers_list or []
        if entry.get("tier") not in SERVICE_TIERS and entry["name"] not in never_shed
    ]


def in_service_tier(containers_list, name, tier):
    """Whether the `containers_list` entry `name` is in `tier`.

    `tier` is one of `SERVICE_TIERS` or a group name from `TIER_GROUPS`.

    Raises ValueError when no entry is named `name`: a renamed or removed entry must fail the
    render rather than read as untiered, which would quietly drop its tile off the email tier.
    """
    if not any(entry["name"] == name for entry in containers_list or []):
        raise ValueError(f"no containers_list entry named {name!r}")
    return any(entry["name"] == name for entry in tier_entries(containers_list, tier))


def tier_priority_class(containers_list, name):
    """The PriorityClass the `containers_list` entry `name` runs at, from its `tier`.

    Raises ValueError when no entry is named `name`, or when it declares no tier: a template
    that asks has no literal to fall back on, and a pod with no class sits at priority 0.
    """
    for entry in containers_list or []:
        if entry["name"] == name:
            declared = entry.get("tier")
            if declared is None:
                raise ValueError(
                    f"containers_list entry {name!r} declares no tier, so it has no "
                    "derived priority class; add `tier:` or pass pod_shell a literal"
                )
            return SERVICE_TIERS[declared]
    raise ValueError(f"no containers_list entry named {name!r}")
