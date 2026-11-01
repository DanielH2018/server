"""M6 batch B5's Pi heartbeat rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the `pi-checks.yaml` share of its `PAGING`
outcome lives here, on the same walk of the rendered routing tree. The SD-health pair is critical,
so it mails as well as posts. The container-recovery pair is a warning, so it posts to Discord
alone, as the "Daniel Pi Recovery" tile did. A rule that regains `shadow` walks to the blackhole.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b5_pi.py
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

PI_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "pi-checks.yaml"

# Each rule and the receivers Alertmanager sends it to.
EXPECTED = {
    "PiSdHealthFailed": ["discord", "email"],
    "PiSdHealthNotReporting": ["discord", "email"],
    "PiRecoveryFailed": ["discord"],
    "PiRecoveryNotReporting": ["discord"],
}


def pi_sets() -> dict[str, dict[str, str]]:
    """The labels each `pi-checks` rule reaches Alertmanager with, keyed by alert name.

    Every rule carries a literal `service`, so its own labels are the whole set.
    """
    [group] = yaml_fast.safe_load(PI_RULES.read_text())["groups"]
    return {
        rule["alert"]: {"alertname": rule["alert"], **rule["labels"]}
        for rule in group["rules"]
    }


def test_the_four_pi_rules_are_the_ones_this_module_expects():
    # Non-vacuity by name: a rule file that lost a rule, or gained one with no expected outcome,
    # fails here instead of walking past the parametrized test below.
    assert set(pi_sets()) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_pi_rule_pages_through_the_muted_routes(name):
    labels = pi_sets()[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[name]
    assert unmuted(labels) == []


def test_a_pi_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    shadowed = {**pi_sets()["PiSdHealthFailed"], "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
