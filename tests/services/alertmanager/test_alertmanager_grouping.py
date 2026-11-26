"""How Alertmanager groups and repeats each alert set: the settings a route inherits.

`test_alertmanager_routing.py` proves which receivers a set reaches. This module reads the
`group_by` and `repeat_interval` each of those routes inherits down the rendered tree. A
host-plane alert groups by `alertname` alone, so one fault on every host is one message. A
warning that holds by design, labelled `escalate: "false"`, repeats daily instead of every 6h.

Run: uv run pytest tests/services/alertmanager/test_alertmanager_grouping.py
"""

import pytest
from lib.inventory_lists import host_names
from test_alertmanager_routing import (
    DEPLOY_HOLD,
    PAGING,
    rule_sets,
    inherited,
    route_tree,
)

HOLDS_BY_DESIGN_MATCHER = 'escalate="false"'


def holds_by_design() -> dict[str, dict[str, str]]:
    """Each paging set whose rule carries `escalate: "false"`, keyed as `PAGING` is."""
    return {
        name: labels
        for name, (labels, _) in PAGING.items()
        if labels.get("escalate") == "false"
    }


@pytest.mark.parametrize("name", sorted(holds_by_design()))
def test_a_warning_that_holds_by_design_repeats_daily(name):
    assert inherited("repeat_interval", holds_by_design()[name]) == ["24h"]


def test_holds_by_design_names_the_deploy_hold_and_every_failed_scan():
    assert DEPLOY_HOLD["escalate"] == "false"
    assert {
        "AideCheckFailed/daniel-box",
        "AideCheckFailed/daniel-server",
        "RkhunterScanFailed/daniel-box",
        "RkhunterScanFailed/daniel-server",
        "RkhunterScanFailed/daniel-pi",
    } <= set(holds_by_design())


def without_daily_repeat(tree: dict) -> dict:
    """`tree` with every route that matches `escalate="false"` removed, at every depth."""
    tree["routes"] = [
        without_daily_repeat(r)
        for r in tree.get("routes", [])
        if HOLDS_BY_DESIGN_MATCHER not in r.get("matchers", [])
    ]
    return tree


def test_a_hold_by_design_without_its_daily_route_is_flagged():
    tree = without_daily_repeat(route_tree())
    assert inherited("repeat_interval", DEPLOY_HOLD, tree) == ["6h"]
    scan, _ = PAGING["RkhunterScanFailed/daniel-pi"]
    assert inherited("repeat_interval", scan, tree) == ["6h"]


@pytest.mark.parametrize("name", sorted(rule_sets("host-checks.yaml", shadow=False)))
def test_a_host_plane_alert_groups_by_alertname_alone(name):
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
    labels, receivers = PAGING[name]
    assert inherited("group_by", labels) == [["alertname"]] * len(receivers)


def test_a_service_alert_groups_by_alertname_and_service():
    labels, _ = PAGING["critical"]
    assert inherited("group_by", labels) == [["alertname", "service"]] * 2


def test_a_host_plane_alert_without_its_grouping_route_is_flagged():
    tree = route_tree()
    tree["routes"] = [r for r in tree["routes"] if r.get("group_by") != ["alertname"]]
    labels, _ = PAGING["RkhunterScanFailed/daniel-pi"]
    assert inherited("group_by", labels, tree) == [["alertname", "service"]]


def host_plane_matcher(tree: dict) -> str:
    [route] = [r for r in tree["routes"] if r.get("group_by") == ["alertname"]]
    [matcher] = route["matchers"]
    return matcher


def test_the_host_plane_route_names_every_inventory_host():
    assert host_plane_matcher(route_tree()) == (f'service=~"{"|".join(host_names())}"')
