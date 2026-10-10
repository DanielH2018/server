"""`k3s_longhorn_r2_volumes` is derived from the `tier` field on containers_list entries (#3389).

The daily R2 tier was a hand-written list in the k3s role's defaults. It is now every
`backup_claims` volume of a `home-critical` entry, one whose tier is `home-edge` or
`home-automation`, computed by `filter_plugins/service_tier.py`. These tests pin the derivation to the list it replaced, and
pin each tiered entry's `backup_claims` to the Longhorn PVCs its role really renders, so the
claims cannot become a second hand list that drifts from the manifests.

Run: uv run pytest ansible/tests/longhorn/test_r2_tier_is_derived.py
"""

import pytest

from _helpers import load_defaults
from _k8s_render import k8s_entries
from lib.service_tiers import R2_TIER, r2_volumes, resolved_tier_lists
from service_tier import tier_backup_claims, tier_entries
from test_every_longhorn_pvc_has_a_tier import longhorn_pvcs_by_role
from lib.repo_paths import K3S_ROLE

# The hand list `k3s_longhorn_r2_volumes` held before the derivation replaced it.
_REPLACED_HAND_LIST = frozenset(
    {
        "homelab/traefik-acme",
        "homelab/authelia-config",
        "homelab/home-assistant-config",
        "homelab/zigbee2mqtt-data",
    }
)

# The entries the first slice of #3389 tagged. A census that finds fewer stopped matching.
_KNOWN_HOME_CRITICAL = frozenset(
    {
        "crowdsec",
        "traefik",
        "authelia",
        "pihole",
        "mosquitto",
        "zigbee2mqtt",
        "home-assistant",
    }
)


def test_the_role_default_is_the_derivation_not_a_list():
    raw = load_defaults(K3S_ROLE)["k3s_longhorn_r2_volumes"]
    assert isinstance(raw, str) and "tier_backup_claims" in raw, raw


def test_derived_r2_list_equals_the_hand_list_it_replaced():
    derived = r2_volumes()
    assert len(derived) == len(set(derived)), derived
    assert set(derived) == _REPLACED_HAND_LIST


def test_every_known_home_critical_entry_is_tagged():
    tagged = {e["name"] for e in tier_entries(list(k8s_entries().values()), R2_TIER)}
    assert _KNOWN_HOME_CRITICAL <= tagged, sorted(_KNOWN_HOME_CRITICAL - tagged)


def test_backup_claims_are_the_tiered_roles_backed_up_longhorn_pvcs():
    """Each tiered entry's claims equal its role's `longhorn`-class PVCs, less the no-backup list.

    A claim its role does not render routes nothing, and a backed-up PVC the entry omits falls
    to the weekly-or-unrouted path; both read as a converged cluster.
    """
    nobackup = set(
        resolved_tier_lists(load_defaults(K3S_ROLE))["k3s_longhorn_nobackup_volumes"]
    )
    by_role = longhorn_pvcs_by_role()
    mismatched = {}
    for entry in tier_entries(list(k8s_entries().values()), R2_TIER):
        declared = set(tier_backup_claims([entry], R2_TIER, "homelab"))
        rendered = by_role.get(entry["name"], set()) - nobackup
        if declared != rendered:
            mismatched[entry["name"]] = (sorted(declared), sorted(rendered))
    assert not mismatched, f"backup_claims (declared, rendered): {mismatched}"


def test_untagging_an_entry_drops_its_claims():
    entries = [
        {"name": "a", "tier": "home-edge", "backup_claims": ["a-config"]},
        {"name": "b", "backup_claims": ["b-config"]},
        {
            "name": "c",
            "tier": "home-automation",
            "namespace": "other",
            "backup_claims": ["c-data"],
        },
    ]
    assert tier_backup_claims(entries, "home-critical", "homelab") == [
        "homelab/a-config",
        "other/c-data",
    ]
    del entries[0]["tier"]
    assert tier_backup_claims(entries, "home-critical", "homelab") == ["other/c-data"]


def test_a_misspelt_tier_is_refused():
    with pytest.raises(ValueError, match="home-critcal"):
        tier_backup_claims(
            [{"name": "a", "tier": "home-critcal"}], "home-critical", "homelab"
        )
