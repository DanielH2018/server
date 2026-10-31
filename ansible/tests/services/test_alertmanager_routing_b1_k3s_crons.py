"""M6 batch B1's rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the `host-checks.yaml` share of its
`PAGING` outcome that belongs to B1 lives here, on the same walk of the rendered routing tree.
The ten k3s-cron rules and `KumaDown` page. The `critical` ones (the longhorn-backup and
etcd-snapshot pairs, and `KumaDown`) mail as well as post, the rest post to Discord alone, and a
rule that regains `shadow` walks to the blackhole instead.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b1_k3s_crons.py
"""

import pytest
from test_alertmanager_routing import (
    BLACKHOLE,
    PAGING,
    SHADOW,
    host_sets,
    not_blackholed,
    reached,
    route_tree,
    unmuted,
)

# Each rule (keyed `<alert>/<service>`) and the receivers Alertmanager sends it to.
EXPECTED = {
    "LonghornBackupHealthFailed/daniel-box": ["discord", "email"],
    "LonghornBackupHealthNotReporting/daniel-box": ["discord", "email"],
    "EtcdSnapshotOffboxFailed/daniel-box": ["discord", "email"],
    "EtcdSnapshotOffboxNotReporting/daniel-box": ["discord", "email"],
    "ManifestPruneCheckFailed/daniel-box": ["discord"],
    "ManifestPruneCheckNotReporting/daniel-box": ["discord"],
    "ReleaseStalenessCheckFailed/daniel-box": ["discord"],
    "ReleaseStalenessCheckNotReporting/daniel-box": ["discord"],
    "LiveDriftCheckFailed/daniel-box": ["discord"],
    "LiveDriftCheckNotReporting/daniel-box": ["discord"],
    "KumaDown/uptime-kuma": ["discord", "email"],
}


def test_the_eleven_b1_rules_are_the_ones_this_module_expects():
    # Non-vacuity by name: a rule dropped from the file, or an expected rule that is still
    # shadow, fails here instead of walking past the parametrized tests below.
    assert set(EXPECTED) <= set(host_sets(shadow=False))
    assert not set(EXPECTED) & set(host_sets(shadow=True))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_b1_rule_pages_through_the_muted_routes(name):
    labels, receivers = PAGING[name]
    assert receivers == EXPECTED[name]
    assert name not in SHADOW
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[name]
    assert unmuted(labels) == []


def test_a_b1_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    labels, _ = PAGING["LonghornBackupHealthFailed/daniel-box"]
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []
