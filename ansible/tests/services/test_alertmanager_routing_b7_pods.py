"""M6 batch B7's `pods` rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the `pods.yaml` share of its `PAGING`
outcome lives here, on the same walk of the rendered routing tree. The two cloudflare-ddns LogQL
rules are in that module's `loki_sets`, which `PAGING` reads. Every `pods` rule pages. The two
autofix-bridge exporter rules are critical, so they mail as well as post; the rest are warnings
that post to Discord alone, the channel mix the four Kuma tiles had. A rule that regains
`shadow` walks to the blackhole instead.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b7_pods.py
"""

import pytest
from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_routing import (
    BLACKHOLE,
    not_blackholed,
    reached,
    route_tree,
    unmuted,
)

PODS_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "pods.yaml"

# Each rule and the receivers Alertmanager sends it to.
EXPECTED = {
    "ArrAutoBlockFailing": ["discord"],
    "AutofixBridgeDown": ["discord", "email"],
    "AutofixBridgeStale": ["discord", "email"],
    "CloudflareDdnsUnavailable": ["discord"],
    "PiPeerBackupFailed": ["discord"],
    "PiPeerBackupNotReporting": ["discord"],
}


def pods_sets() -> dict[str, dict[str, str]]:
    """The labels each `pods` rule reaches Alertmanager with, keyed by alert name."""
    [group] = yaml_fast.safe_load(PODS_RULES.read_text())["groups"]
    return {
        rule["alert"]: {"alertname": rule["alert"], **rule["labels"]}
        for rule in group["rules"]
    }


def test_the_six_pods_rules_are_the_ones_this_module_expects():
    # Non-vacuity by name: a rule file that lost a rule, or gained one with no expected outcome,
    # fails here instead of walking past the parametrized test below.
    assert set(pods_sets()) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_pods_rule_pages_through_the_muted_routes(name):
    labels = pods_sets()[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[name]
    assert unmuted(labels) == []


def test_a_pods_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    shadowed = {**pods_sets()["AutofixBridgeDown"], "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
