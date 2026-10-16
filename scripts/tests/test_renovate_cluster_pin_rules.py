"""The k3s, MetalLB and Longhorn Renovate rules, split by H1.1g, and the `coredns_image` disable.

After H1, merging the MetalLB or Longhorn pin is the upgrade (H1 Open question 6), so each of
those rules names the gate `land.sh` runs at the merge, while k3s stays a hand apply. None of
the three automerges. The assertions resolve through `_resolve_setting`, which walks the
packageRules in order, because a later rule that sets the same field would override the
rule's own body. The one exception reads the k3s rule's own description, which the schema-cap
rule after it overrides.

Run: uv run pytest scripts/tests/test_renovate_cluster_pin_rules.py
"""

import pytest
from _renovate import _CONFIG_MANAGERS, _PACKAGE_RULES, _resolve_setting
from lib.renovate_annotations import is_annotation_manager

K3S_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"
DEP_TYPE = next(m for m in _CONFIG_MANAGERS if is_annotation_manager(m))[
    "depTypeTemplate"
]

# depName -> the text its group name and description must carry.
GATED = {
    "metallb/metallb": "metallb-upgrade",
    "longhorn/longhorn": "longhorn-upgrade",
}
PINS = ("k3s-io/k3s", *GATED)


def _setting(
    field: str, dep: str, path: str = K3S_DEFAULTS, datasource="github-releases"
):
    return _resolve_setting(field, dep, path, "minor", datasource, dep_type=DEP_TYPE)


@pytest.mark.parametrize("dep", PINS)
def test_no_cluster_pin_automerges(dep):
    assert _setting("automerge", dep) is False


def test_each_cluster_pin_has_a_group_of_its_own():
    groups = {dep: _setting("groupName", dep) for dep in PINS}
    assert len(set(groups.values())) == len(PINS), groups
    assert all("(manual —" in g for g in groups.values()), groups


@pytest.mark.parametrize(("dep", "gate"), GATED.items())
def test_a_gated_pin_names_its_merge_gate(dep, gate):
    assert gate in _setting("groupName", dep)
    description = _setting("description", dep)
    assert "land.sh" in description and "homelab/upgrade-gates" in description


def test_the_k3s_pin_names_its_hand_apply():
    group = _setting("groupName", "k3s-io/k3s")
    assert "docs/k3s-upgrade.md" in group
    # The schema-cap rule after it sets its own description, so read the rule that sets
    # the group rather than the resolved field.
    [rule] = [r for r in _PACKAGE_RULES if r.get("groupName") == group]
    assert "H6" in rule["description"]


def test_the_coredns_image_in_a_k8s_role_is_disabled():
    path = "ansible/roles/k8s/coredns/defaults/main.yml"
    dep = "rancher/mirrored-coredns-coredns"
    assert _setting("enabled", dep, path, "docker") is False


def test_the_coredns_release_annotation_stays_tracked():
    assert _setting("enabled", "coredns/coredns") is None
