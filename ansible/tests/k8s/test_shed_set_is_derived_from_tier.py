"""The lost-node shed set is the untiered entries, derived from containers_list `tier` (#3481).

`shed_entries` in `filter_plugins/service_tier.py` derives it and `probe.py shed-set` prints
it. Scaling these to zero is meant to leave the tiered services untouched, which holds only
while no tiered entry depends on a shed one.

Run: uv run pytest ansible/tests/k8s/test_shed_set_is_derived_from_tier.py
"""

import pytest

from _k8s_render import k8s_entries
from lib.service_tiers import shed_set
from service_tier import SERVICE_TIERS, shed_entries


def _entry(name, tier=None, **extra):
    entry = {"name": name, "platform": "k8s", **extra}
    if tier is not None:
        entry["tier"] = tier
    return entry


def _tiered_dependencies_on_shed(entries):
    """`(tiered, shed)` for every `depends_on` edge from a tiered entry to a shed one."""
    shed = {e["name"] for e in shed_entries(entries)}
    return [
        (e["name"], dep)
        for e in entries
        if e.get("tier")
        for dep in e.get("depends_on") or []
        if dep in shed
    ]


def test_an_untiered_entry_is_shed_and_every_tier_is_kept():
    entries = [_entry("sonarr"), *(_entry(f"svc-{t}", t) for t in SERVICE_TIERS)]
    assert [e["name"] for e in shed_entries(entries)] == ["sonarr"]


def test_a_misspelt_tier_raises_rather_than_shedding_the_service():
    with pytest.raises(ValueError, match="unknown tier 'home-edg'"):
        shed_entries([_entry("traefik", "home-edg")])


def test_the_inventory_sheds_an_arr_and_keeps_every_tiered_service():
    names = shed_set()
    assert "sonarr" in names
    tiered = {n for n, e in k8s_entries().items() if e.get("tier")}
    assert len(tiered) >= 11
    assert not tiered & set(names)


def test_a_tiered_dependency_on_a_shed_entry_is_flagged():
    entries = [
        _entry("loki-homelab"),
        _entry("monitor-bridge", "platform", depends_on=["loki-homelab"]),
    ]
    assert _tiered_dependencies_on_shed(entries) == [("monitor-bridge", "loki-homelab")]


def test_no_tiered_entry_depends_on_a_shed_entry():
    assert _tiered_dependencies_on_shed(list(k8s_entries().values())) == []
