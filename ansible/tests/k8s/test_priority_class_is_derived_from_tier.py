"""A tiered role's PriorityClass is derived from its containers_list `tier`, never written (#3452).

Each pod template used to pass `pod_shell(...)` a literal class, which ranked the same services a
second time beside their `tier`. A tiered role now passes `containers_list |
tier_priority_class('<role>')`, and `SERVICE_TIERS` in `filter_plugins/service_tier.py` maps each
tier to one class.

Run: uv run pytest ansible/tests/k8s/test_priority_class_is_derived_from_tier.py
"""

import re

import pytest

from _helpers import K8S_ROLES
from _k8s_render import k8s_entries
from service_tier import SERVICE_TIERS, in_service_tier, tier_priority_class

# The class each tiered role's pods ran at when the literals were replaced. A tier edit that
# moves one of these is a priority change, and should fail here before it reaches the cluster.
_CLASS_AT_DERIVATION = {
    "crowdsec": "homelab-critical",
    "traefik": "homelab-critical",
    "authelia": "homelab-critical",
    "pihole": "homelab-critical",
    "registry": "homelab-critical",
    "monitor-bridge": "homelab-critical",
    "nut": "homelab-critical",
    "wg-easy": "homelab-critical",
    "mosquitto": "homelab-automation",
    "zigbee2mqtt": "homelab-automation",
    "home-assistant": "homelab-automation",
}

_LITERAL_CLASS = re.compile(r"pod_shell\(\s*['\"]")


def _tiered_roles() -> dict[str, dict]:
    return {n: e for n, e in k8s_entries().items() if e.get("tier")}


def test_every_tiered_role_derives_the_class_it_ran_at():
    entries = list(k8s_entries().values())
    derived = {name: tier_priority_class(entries, name) for name in _tiered_roles()}
    assert derived == _CLASS_AT_DERIVATION


def literal_class_calls(template_text: str) -> int:
    """How many `pod_shell(...)` calls in `template_text` pass a quoted literal class."""
    return len(_LITERAL_CLASS.findall(template_text))


def test_literal_class_calls_is_clean_on_a_derived_call():
    text = "{{ pod_shell(containers_list | tier_priority_class('nut'), fs_group=1) }}"
    assert literal_class_calls(text) == 0


def test_literal_class_calls_is_flagged_on_a_literal():
    assert literal_class_calls("{{ pod_shell('homelab-critical') }}") == 1


def test_no_tiered_role_passes_pod_shell_a_literal_class():
    offenders = {}
    for name in _tiered_roles():
        for tpl in sorted((K8S_ROLES / name / "templates").glob("*.j2")):
            if literal_class_calls(tpl.read_text()):
                offenders.setdefault(name, []).append(tpl.name)
    assert not offenders, (
        f"pod_shell() given a literal class in a tiered role: {offenders}"
    )


def test_tier_priority_class_refuses_an_untiered_entry():
    with pytest.raises(ValueError, match="declares no tier"):
        tier_priority_class([{"name": "a"}], "a")


def test_tier_priority_class_refuses_a_missing_entry():
    with pytest.raises(ValueError, match="no containers_list entry named 'b'"):
        tier_priority_class([{"name": "a", "tier": "platform"}], "b")


def test_in_service_tier_selects_by_tier_group():
    entries = [{"name": "edge", "tier": "home-edge"}, {"name": "plain"}]
    assert in_service_tier(entries, "edge", "home-critical")
    assert not in_service_tier(entries, "plain", "home-critical")


def test_in_service_tier_refuses_a_missing_entry():
    with pytest.raises(ValueError, match="no containers_list entry named 'b'"):
        in_service_tier([{"name": "a", "tier": "home-edge"}], "b", "home-critical")


def test_a_tier_group_is_not_a_declarable_tier():
    """`home-critical` selects tiers; an entry declaring it would have no single class."""
    with pytest.raises(ValueError, match="unknown tier 'home-critical'"):
        tier_priority_class([{"name": "a", "tier": "home-critical"}], "a")


def test_every_tier_names_a_declared_priority_class():
    from test_pod_template_hygiene import _homelab_tiers

    assert set(SERVICE_TIERS.values()) <= _homelab_tiers()
