"""M6 batch B8's probe rules reach Alertmanager's receivers: they page since Cutover-b.

`test_alertmanager_routing.py` is at its line cap, so the `extra-probes.yaml` share of its
`PAGING` outcome lives here, on the same walk of the rendered routing tree. Every rule pages:
`ExtraProbeFailedCritical` is critical, so it mails as well as posts, the rest are warnings that
post to Discord alone, and a rule that regains `shadow` walks to the blackhole instead.

The per-target rules take `service` from the probe series, so their rule labels carry none. A
stand-in `service` fills it, as `INGRESS_STAND_IN_SERVICE` does for the ingress rules. No route
matches on `service`, so the stand-in cannot move a set.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b8_probes.py
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

EXTRA_PROBES = ROLES / "k8s" / "observability" / "files" / "rules" / "extra-probes.yaml"
STAND_IN_SERVICE = "mosquitto"

# Each rule and the receivers Alertmanager sends it to.
EXPECTED = {
    "ExtraProbeFailedCritical": ["discord", "email"],
    "ExtraProbeFailed": ["discord"],
    "PiPortDead": ["discord"],
    "PiHostDown": ["discord"],
    "ExtraProbesMissing": ["discord"],
}


def probe_sets() -> dict[str, dict[str, str]]:
    """The labels each `extra-probes` rule reaches Alertmanager with, keyed by alert name."""
    [group] = yaml_fast.safe_load(EXTRA_PROBES.read_text())["groups"]
    return {
        rule["alert"]: {
            "alertname": rule["alert"],
            "service": STAND_IN_SERVICE,
            **rule["labels"],
        }
        for rule in group["rules"]
    }


def test_the_five_probe_rules_are_the_ones_this_module_expects():
    # Non-vacuity by name: a rule file that lost a rule, or gained one with no expected outcome,
    # fails here instead of walking past the parametrized test below.
    assert set(probe_sets()) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_probe_rule_pages_through_the_muted_routes(name):
    labels = probe_sets()[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[name]
    assert unmuted(labels) == []


def test_a_probe_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    shadowed = {**probe_sets()["ExtraProbeFailedCritical"], "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
