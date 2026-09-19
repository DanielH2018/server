"""Ansible filter plugin deriving Authelia's per-service access_control rules from containers_list.

Every `use_authelia: true` entry declares its own `auth_tier` beside its hostname, and the
Authelia config template renders the per-service rules from that declaration. Until 2026-09-18
the policy lived in the template alone, joined to the inventory by a hostname string: four
services had a rule and the other 24 rode the `*.local.<domain>` one_factor wildcard by
omission (#2057). An entry that attaches the middleware but declares no tier now fails the
render — here, in `validate/k8s_manifests.py`, and in a real deploy alike — so the fall-through
has no representation.
"""

from ansible.errors import AnsibleFilterError

# The policies a service can declare, in the order their rules render (first match wins, so
# the stricter tier goes first). `bypass` is deliberately absent: a scoped bypass
# (paths, networks, methods) has no per-service shape and stays a hand-written rule in the
# template, ahead of the generated block, while the entry's `auth_tier` names what its UI
# traffic gets.
AUTH_TIERS = ("two_factor", "one_factor")


def authelia_service_rules(containers_list, domain, one_factor_networks):
    """One access_control rule per tier, listing every `use_authelia: true` entry's LAN name.

    Args:
        containers_list: the host's full containers_list.
        domain: the zone the `.local.` names hang off.
        one_factor_networks: the source networks the `one_factor` rule is scoped to — the same
            RFC1918 list the `*.local.<domain>` wildcard carries, so a service's effective
            policy is unchanged by having its own rule.

    Returns:
        A list of rule mappings in first-match order: `two_factor` first, then `one_factor`,
        each with its `domain` list sorted. The output is a pure function of the sorted
        entries, so a reorder of containers_list does not churn the rendered Secret and roll
        Authelia. A tier with no member renders no rule.

    Raises:
        AnsibleFilterError: a `use_authelia: true` entry without `auth_tier` or `hostname`, an
            `auth_tier` outside AUTH_TIERS, or an `auth_tier` on an entry that does not attach
            the middleware — that declaration would gate nothing.
    """
    by_tier = {tier: [] for tier in AUTH_TIERS}
    for entry in containers_list:
        name = entry.get("name", "<unnamed>")
        tier = entry.get("auth_tier")
        if not entry.get("use_authelia"):
            if tier is not None:
                raise AnsibleFilterError(
                    f"containers_list entry {name!r} declares auth_tier: {tier} but not "
                    f"use_authelia: true — the tier gates nothing without the middleware"
                )
            continue
        if tier is None:
            raise AnsibleFilterError(
                f"containers_list entry {name!r} has use_authelia: true but no auth_tier — "
                f"declare one of {list(AUTH_TIERS)} beside its hostname"
            )
        if tier not in AUTH_TIERS:
            raise AnsibleFilterError(
                f"containers_list entry {name!r} declares auth_tier: {tier!r}; "
                f"expected one of {list(AUTH_TIERS)}"
            )
        hostname = entry.get("hostname")
        if not hostname:
            raise AnsibleFilterError(
                f"containers_list entry {name!r} has use_authelia: true but no hostname — "
                f"there is no route for an access_control rule to name"
            )
        by_tier[tier].append(f"{hostname}.local.{domain}")

    rules = []
    for tier in AUTH_TIERS:
        if not by_tier[tier]:
            continue
        rule = {"domain": sorted(by_tier[tier]), "policy": tier}
        if tier == "one_factor":
            rule["networks"] = list(one_factor_networks)
        rules.append(rule)
    return rules


class FilterModule:
    """Ansible filter plugin registering the Authelia access_control helper."""

    def filters(self):
        return {"authelia_service_rules": authelia_service_rules}
