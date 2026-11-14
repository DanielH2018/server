"""M6 batch A6's bridge-exporter rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` builds `PAGING` and `SHADOW` from the rule files. This module
holds the non-vacuity tests for the `bridge-exporter.yaml` share of them: all 21 rules page, the
nine `critical` ones mail as well as post, and a rule that regains `shadow` walks to the
blackhole instead.

Run: uv run pytest tests/services/test_alertmanager_routing_a6_bridge.py
"""

from test_alertmanager_routing import (
    BLACKHOLE,
    BRIDGE_RULES,
    PAGING,
    SHADOW,
    literal_sets,
    not_blackholed,
    reached,
    route_tree,
)

CRITICAL = {
    "WANUnreachable",
    "DiscordDeliveryFailing",
    "R2FreeTierHeadroomLow",
    "MonitorBridgeDown",
    "BridgeCheckStale",
    "B2Unreachable",
    "B2FreeTierHeadroomLow",
    "SmartHealthFailing",
    "DomainRegistrationExpiring",
}


def test_every_a6_bridge_rule_pages():
    """Non-vacuity for the A6 sets: 21 rules, none in `SHADOW`, the critical ones on both
    channels and the rest on Discord alone."""
    assert literal_sets(BRIDGE_RULES, shadow=True) == {}
    sets = literal_sets(BRIDGE_RULES, shadow=False)
    assert len(sets) == 21
    assert {labels["alertname"] for labels in sets.values()} >= CRITICAL
    for key, labels in sets.items():
        assert key in PAGING and key not in SHADOW, key
        critical = labels["alertname"] in CRITICAL
        assert labels["severity"] == ("critical" if critical else "warning"), key
        assert PAGING[key][1] == (["discord", "email"] if critical else ["discord"]), (
            key
        )


def test_an_a6_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    labels = literal_sets(BRIDGE_RULES, shadow=False)["B2Unreachable/b2"]
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
