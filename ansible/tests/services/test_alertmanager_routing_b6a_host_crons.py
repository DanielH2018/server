"""M6 batch B6a's four host-cron rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the B6a share of its `host-checks.yaml`
walk is pinned here, by name, on the same walk of the rendered routing tree. Both tiles
notified Discord only, so every rule is a warning that posts to Discord alone. A rule that
regains `shadow` walks to the blackhole instead.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b6a_host_crons.py
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

HOST_CHECKS = ROLES / "k8s" / "observability" / "files" / "rules" / "host-checks.yaml"

# Each rule and the receivers Alertmanager sends it to.
EXPECTED = {
    "ArtifactsSyncFailed": ["discord"],
    "ArtifactsSyncNotReporting": ["discord"],
    "OtlpDoorClosed": ["discord"],
    "OtlpDoorNotReporting": ["discord"],
}


def b6a_sets() -> dict[str, dict[str, str]]:
    """The labels each B6a rule reaches Alertmanager with, keyed by alert name."""
    [group] = yaml_fast.safe_load(HOST_CHECKS.read_text())["groups"]
    return {
        rule["alert"]: {"alertname": rule["alert"], **rule["labels"]}
        for rule in group["rules"]
        if rule["alert"] in EXPECTED
    }


def test_the_four_b6a_rules_are_the_ones_this_module_expects():
    # Non-vacuity by name: a rule file that lost a rule fails here instead of walking past the
    # parametrized test below.
    assert set(b6a_sets()) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_b6a_rule_pages_through_the_muted_routes(name):
    labels = b6a_sets()[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[name]
    assert unmuted(labels) == []


def test_a_b6a_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    shadowed = {**b6a_sets()["OtlpDoorClosed"], "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
