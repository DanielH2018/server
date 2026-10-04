"""Ansible filter plugin deriving per-tier sets from the `tier` field on containers_list entries.

A service's tier is declared once, on its `containers_list` entry, and every set that ranks
services is derived from it (#3389). `tier_backup_claims` is the first consumer: the k3s role's
`k3s_longhorn_r2_volumes`, the volumes backed up daily to R2, is every `backup_claims` volume of
a `home-critical` entry.

No Ansible import, so `scripts/lib/service_tiers.py` and the docs generators read the same
function the playbook runs. Ansible wraps a filter's `ValueError` in its own error.
"""

# The tiers an entry's `tier` may name. A misspelt tier would silently drop the entry out of
# every derived set, so an unknown value raises rather than reading as untiered.
SERVICE_TIERS = ("home-critical",)


def tier_entries(containers_list, tier):
    """The named entries of `containers_list` whose `tier` is `tier`, in list order.

    Raises ValueError when `tier`, or any entry's `tier`, is not in `SERVICE_TIERS`.
    """
    if tier not in SERVICE_TIERS:
        raise ValueError(
            f"unknown service tier {tier!r}; known: {', '.join(SERVICE_TIERS)}"
        )
    found = []
    for entry in containers_list or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        declared = entry.get("tier")
        if declared is not None and declared not in SERVICE_TIERS:
            raise ValueError(
                f"containers_list entry {entry['name']!r} declares unknown tier {declared!r}; "
                f"known: {', '.join(SERVICE_TIERS)}"
            )
        if declared == tier:
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


class FilterModule:
    def filters(self):
        return {"tier_backup_claims": tier_backup_claims}
