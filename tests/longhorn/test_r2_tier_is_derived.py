"""`k3s_longhorn_r2_volumes` is derived from the containers_list entries.

The daily R2 tier was a hand-written list in the k3s role's defaults. It is now every
`backup_claims` volume of a `home-critical` entry, one whose tier is `home-edge` or
`home-automation`, then every entry's `r2_backup_claims`, computed by
`template_filters/longhorn_groups.py`. These tests pin the derivation to the list it replaced
plus the declared claims, and pin every claim to a Longhorn PVC its role really renders, so the
claims cannot become a second hand list that drifts from the manifests.

Run: uv run pytest tests/longhorn/test_r2_tier_is_derived.py
"""

import pytest

from _helpers import load_defaults
from _k8s_render import k8s_entries
from lib.service_tiers import r2_volumes, resolved_tier_lists
from renderer.template_filters.longhorn_groups import R2_TIER, longhorn_r2_claims
from renderer.template_filters.service_tier import tier_entries
from test_every_longhorn_pvc_has_a_tier import longhorn_pvcs_by_role
from lib.repo_paths import LONGHORN_RECONCILE_ROLE

# The hand list `k3s_longhorn_r2_volumes` held, which the home-critical half must reproduce.
_REPLACED_HAND_LIST = frozenset(
    {
        "homelab/traefik-acme",
        "homelab/authelia-config",
        "homelab/home-assistant-config",
        "homelab/zigbee2mqtt-data",
    }
)

# The `r2_backup_claims` of untiered entries. ADR 0007 names each and why.
_DECLARED_R2 = frozenset({"homelab/airflow-postgres-data", "homelab/vaultwarden-data"})

# The tagged entries. A census that finds fewer stopped matching.
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
    raw = load_defaults(LONGHORN_RECONCILE_ROLE)["k3s_longhorn_r2_volumes"]
    assert isinstance(raw, str) and "longhorn_r2_claims" in raw, raw


def test_derived_r2_list_is_the_replaced_hand_list_plus_the_declared_claims():
    derived = r2_volumes()
    assert len(derived) == len(set(derived)), derived
    assert set(derived) == _REPLACED_HAND_LIST | _DECLARED_R2


def test_every_known_home_critical_entry_is_tagged():
    tagged = {e["name"] for e in tier_entries(list(k8s_entries().values()), R2_TIER)}
    assert _KNOWN_HOME_CRITICAL <= tagged, sorted(_KNOWN_HOME_CRITICAL - tagged)


def test_backup_claims_are_the_tiered_roles_backed_up_longhorn_pvcs():
    """Each tiered entry's claims equal its role's `longhorn`-class PVCs, less the no-backup list.

    A claim its role does not render routes nothing, and a backed-up PVC the entry omits falls
    to the weekly-or-unrouted path; both read as a converged cluster.
    """
    nobackup = set(
        resolved_tier_lists(load_defaults(LONGHORN_RECONCILE_ROLE))[
            "k3s_longhorn_nobackup_volumes"
        ]
    )
    by_role = longhorn_pvcs_by_role()
    mismatched = {}
    for entry in tier_entries(list(k8s_entries().values()), R2_TIER):
        declared = set(longhorn_r2_claims([entry], "homelab"))
        rendered = by_role.get(entry["name"], set()) - nobackup
        if declared != rendered:
            mismatched[entry["name"]] = (sorted(declared), sorted(rendered))
    assert not mismatched, f"backup_claims (declared, rendered): {mismatched}"


def test_every_r2_backup_claim_is_a_longhorn_pvc_its_role_renders():
    by_role = longhorn_pvcs_by_role()
    unrendered = {
        entry["name"]: sorted(missing)
        for entry in k8s_entries().values()
        if (
            missing := set(longhorn_r2_claims([{**entry, "tier": None}], "homelab"))
            - by_role.get(entry["name"], set())
        )
    }
    assert not unrendered, (
        f"r2_backup_claims with no rendered longhorn PVC: {unrendered}"
    )


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
    assert longhorn_r2_claims(entries, "homelab") == [
        "homelab/a-config",
        "other/c-data",
    ]
    del entries[0]["tier"]
    assert longhorn_r2_claims(entries, "homelab") == ["other/c-data"]


def test_an_untiered_entrys_r2_backup_claims_join_the_list():
    entries = [
        {"name": "a", "tier": "home-edge", "backup_claims": ["a-config"]},
        {"name": "w", "namespace": "data", "r2_backup_claims": ["w-db"]},
    ]
    assert longhorn_r2_claims(entries, "homelab") == ["homelab/a-config", "data/w-db"]


def test_a_claim_declared_twice_is_refused():
    entries = [
        {"name": "a", "tier": "home-edge", "backup_claims": ["a-config"]},
        {"name": "a2", "r2_backup_claims": ["a-config"]},
    ]
    with pytest.raises(ValueError, match="homelab/a-config is declared twice"):
        longhorn_r2_claims(entries, "homelab")
