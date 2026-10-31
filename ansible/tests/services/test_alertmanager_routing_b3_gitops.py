"""M6 batch B3's GitHub rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the `gitops-checks.yaml` share of its
paging outcome lives here, on the same walk of the rendered routing tree. The four GitHub rules
are warnings, so they post to Discord alone. The two `GitOpsTick*` rules in the same group are not
B3's and stay shadow, so they walk to the blackhole. A rule that regains `shadow` does too.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b3_gitops.py
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

GITOPS_CHECKS = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "gitops-checks.yaml"
)

# The four B3 rules and the receivers Alertmanager sends each to.
PAGING_RULES = {
    "GitHubRulesetDrift": ["discord"],
    "GitHubRulesetDriftNotReporting": ["discord"],
    "GitHubInteractionLimitNotArmed": ["discord"],
    "GitHubInteractionLimitNotReporting": ["discord"],
}
# The rules that share the group and stay shadow until the operator promotes them.
SHADOW_RULES = {"GitOpsTickStale", "GitOpsTickOutcomeRepeating"}


def gitops_sets() -> dict[str, dict[str, str]]:
    """The labels each `gitops-checks` rule reaches Alertmanager with, keyed by alert name."""
    [group] = yaml_fast.safe_load(GITOPS_CHECKS.read_text())["groups"]
    return {
        rule["alert"]: {"alertname": rule["alert"], **rule["labels"]}
        for rule in group["rules"]
    }


def test_the_group_holds_exactly_the_b3_rules_and_the_two_shadow_rules():
    # Non-vacuity by name: a rule file that lost a rule, or gained one with no expected outcome,
    # fails here instead of walking past the parametrized tests below.
    assert set(gitops_sets()) == set(PAGING_RULES) | SHADOW_RULES


@pytest.mark.parametrize("name", sorted(PAGING_RULES))
def test_every_b3_rule_pages_through_the_muted_routes(name):
    labels = gitops_sets()[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == PAGING_RULES[name]
    assert unmuted(labels) == []


@pytest.mark.parametrize("name", sorted(SHADOW_RULES))
def test_the_gitops_tick_rules_still_walk_to_the_blackhole(name):
    labels = gitops_sets()[name]
    assert labels["shadow"] == "true"
    assert [r["receiver"] for r in reached(route_tree(), labels)] == [BLACKHOLE]


def test_a_b3_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    shadowed = {**gitops_sets()["GitHubRulesetDrift"], "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
