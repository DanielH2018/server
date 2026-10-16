"""The lost-node shed set is the untiered entries, derived from containers_list `tier` (#3481).

`shed_entries` in `filter_plugins/service_tier.py` derives it and `probe.py shed-set` prints
it. Scaling these to zero is meant to leave the tiered services untouched, which holds only
while no tiered entry depends on a shed one. `NEVER_SHED` keeps the untiered cluster plumbing
out of it, and it must equal the untiered roles that set `flux_prune: false` (H1.1d).

Run: uv run pytest ansible/tests/k8s/test_shed_set_is_derived_from_tier.py
"""

import pytest

from _k8s_render import k8s_entries
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from lib.service_tiers import shed_set
from service_tier import NEVER_SHED, SERVICE_TIERS, shed_entries


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


def _never_shed_drift(entries, roles_dir, never_shed):
    """`(missing, extra)` between `never_shed` and the untiered roles that never prune.

    `missing` is each untiered entry whose `roles_dir/<name>/defaults/main.yml` sets
    `flux_prune: false` and that `never_shed` lacks. `extra` is each member that is not one.
    """
    unpruned = set()
    for entry in entries:
        if entry.get("tier"):
            continue
        defaults = roles_dir / entry["name"] / "defaults" / "main.yml"
        if (
            defaults.is_file()
            and (yaml_fast.safe_load(defaults.read_text()) or {}).get("flux_prune")
            is False
        ):
            unpruned.add(entry["name"])
    return sorted(unpruned - never_shed), sorted(never_shed - unpruned)


def _role(roles_dir, name, **defaults):
    (roles_dir / name / "defaults").mkdir(parents=True)
    (roles_dir / name / "defaults" / "main.yml").write_text(
        "".join(f"{key}: {str(value).lower()}\n" for key, value in defaults.items())
    )


def test_a_never_shed_entry_is_dropped_and_other_plumbing_is_kept():
    entries = [_entry("coredns"), _entry("metallb"), _entry("sonarr")]
    shed = shed_entries(entries, never_shed=frozenset({"coredns"}))
    assert [e["name"] for e in shed] == ["metallb", "sonarr"]


def test_an_unpruned_untiered_role_missing_from_never_shed_is_flagged(tmp_path):
    _role(tmp_path, "coredns", flux_prune=False)
    _role(tmp_path, "sonarr")
    entries = [_entry("coredns"), _entry("sonarr")]
    assert _never_shed_drift(entries, tmp_path, frozenset()) == (["coredns"], [])


def test_a_never_shed_member_whose_role_prunes_is_flagged(tmp_path):
    _role(tmp_path, "sonarr", flux_prune=True)
    entries = [_entry("sonarr")]
    assert _never_shed_drift(entries, tmp_path, frozenset({"sonarr"})) == (
        [],
        ["sonarr"],
    )


def test_a_tiered_unpruned_role_outside_never_shed_is_clean(tmp_path):
    """traefik and `flux` set `flux_prune: false` from D5 and keep their tier instead."""
    _role(tmp_path, "traefik", flux_prune=False)
    _role(tmp_path, "coredns", flux_prune=False)
    entries = [_entry("traefik", "home-edge"), _entry("coredns")]
    assert _never_shed_drift(entries, tmp_path, frozenset({"coredns"})) == ([], [])


def test_never_shed_matches_the_untiered_roles_that_never_prune():
    """The real tree. Both sides are empty until H1.2 adds `cluster-base` to each."""
    entries = list(k8s_entries().values())
    assert sum(not e.get("tier") for e in entries) >= 20
    assert _never_shed_drift(entries, K8S_ROLES, NEVER_SHED) == ([], [])
