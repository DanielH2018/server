"""M6 batches B2, B4 and B6b's rules reach Alertmanager's receivers: they page since Cutover-1b.

`test_alertmanager_routing.py` is at its line cap, so the share of its paging outcome that
belongs to `initial-setup-checks.yaml`, `other-setup-checks.yaml` and `pod-producers.yaml` lives
here, on the same walk of the rendered routing tree. Every rule in the three files pages. The
`critical` ones (both UPS secondaries, the full etcd restore drill, the CrowdSec AppSec verify,
the CrowdSec stale-job and exporter-absent rules) mail as well as post. The rest are warnings that
post to Discord alone, the channel mix the 20 Kuma tiles had. A rule that regains `shadow` walks
to the blackhole instead.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing_b2_b4_b6b.py
"""

import pytest
from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_routing import (
    BLACKHOLE,
    literal_sets,
    not_blackholed,
    reached,
    route_tree,
    unmuted,
)

RULES_DIR = ROLES / "k8s" / "observability" / "files" / "rules"
FILES = {
    "b2": RULES_DIR / "initial-setup-checks.yaml",
    "b4": RULES_DIR / "other-setup-checks.yaml",
    "b6b": RULES_DIR / "pod-producers.yaml",
}

BOTH = ["discord", "email"]
DISCORD = ["discord"]
# Each rule (keyed `<alert>/<service>`) and the receivers Alertmanager sends it to.
EXPECTED = {
    "b2": {
        "DocsRefreshFailed/daniel-box": DISCORD,
        "DocsRefreshNotReporting/daniel-box": DISCORD,
        "EvalSweepFailed/daniel-box": DISCORD,
        "EvalSweepNotReporting/daniel-box": DISCORD,
        "SecretRotationAuditFailed/daniel-box": DISCORD,
        "SecretRotationAuditNotReporting/daniel-box": DISCORD,
        "SetupDriftFailed/daniel-server": DISCORD,
        "SetupDriftNotReporting/daniel-server": DISCORD,
        "LokiReadRouteFailed/daniel-box": DISCORD,
        "LokiReadRouteNotReporting/daniel-box": DISCORD,
        "LokiReadRouteFailed/daniel-server": DISCORD,
        "LokiReadRouteNotReporting/daniel-server": DISCORD,
    },
    "b4": {
        "UpsSecondaryFailed/daniel-box": BOTH,
        "UpsSecondaryNotReporting/daniel-box": BOTH,
        "UpsSecondaryFailed/daniel-server": BOTH,
        "UpsSecondaryNotReporting/daniel-server": BOTH,
        "FakeRemuxScanFailed/daniel-box": DISCORD,
        "FakeRemuxScanNotReporting/daniel-box": DISCORD,
        "FakeRemuxReplaceFailed/daniel-box": DISCORD,
        "FakeRemuxReplaceNotReporting/daniel-box": DISCORD,
        "MkvAttachmentRepairFailed/daniel-box": DISCORD,
        "MkvAttachmentRepairNotReporting/daniel-box": DISCORD,
        "EtcdRestoreDrillFailed/daniel-server": BOTH,
        "EtcdRestoreDrillNotReporting/daniel-server": BOTH,
        "EtcdRestoreDrillEgressFenceFailed/daniel-server": DISCORD,
        "EtcdRestoreDrillEgressFenceNotReporting/daniel-server": DISCORD,
        "RenovateNotifyNotReporting/daniel-box": DISCORD,
        "RenovateAgentNotReporting/daniel-box": DISCORD,
    },
    "b6b": {
        "CrowdSecRemoteAllowlistFailed/crowdsec": DISCORD,
        "CrowdSecAppSecVerifyFailed/crowdsec": BOTH,
        "CrowdSecHomeAllowlistFailed/crowdsec": DISCORD,
        "CrowdSecJobStale/crowdsec": BOTH,
        "CrowdSecJobsExporterAbsent/crowdsec": BOTH,
        "ConfigarrSyncFailed/configarr": DISCORD,
        "JanitorrErrorsFound/janitorr": DISCORD,
        "ConfigarrHealthNotReporting/configarr": DISCORD,
        "JanitorrHealthNotReporting/janitorr": DISCORD,
    },
}
CASES = sorted((batch, name) for batch, rules in EXPECTED.items() for name in rules)


@pytest.mark.parametrize("batch", sorted(FILES))
def test_each_file_holds_exactly_the_rules_this_module_expects(batch):
    # Non-vacuity by name: a rule dropped from the file, or added with no expected outcome,
    # fails here instead of walking past the parametrized tests below. The shadow set is empty.
    assert set(literal_sets(FILES[batch], shadow=False)) == set(EXPECTED[batch])
    assert literal_sets(FILES[batch], shadow=True) == {}


@pytest.mark.parametrize(("batch", "name"), CASES)
def test_every_rule_pages_through_the_muted_routes(batch, name):
    labels = literal_sets(FILES[batch], shadow=False)[name]
    assert "shadow" not in labels
    assert [r["receiver"] for r in reached(route_tree(), labels)] == EXPECTED[batch][
        name
    ]
    assert unmuted(labels) == []


def test_a_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    expected for the rule no longer match."""
    labels = literal_sets(FILES["b4"], shadow=False)[
        "EtcdRestoreDrillFailed/daniel-server"
    ]
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
    assert not_blackholed(shadowed) == []


def test_the_files_still_parse_as_one_group_each():
    for path in FILES.values():
        assert len(yaml_fast.safe_load(path.read_text())["groups"]) == 1
