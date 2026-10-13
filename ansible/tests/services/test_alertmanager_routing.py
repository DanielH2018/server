"""Every route an alert from the M2 family reaches carries the `weekly-reboot` mute.

`test_alertmanager_reboot_mute.py` proves the interval covers the reboot. This module proves
the alerts reach it: it walks the rendered routing tree of `alertmanager.yml` the way
Alertmanager does, children in order, stopping at the first match unless `continue: true`,
and reads `mute_time_intervals` on each route the walk ends on. The label sets stand for the
k8s-workloads alerts once they drop `shadow`; later families add theirs to `PAGING`.

Two rejecting halves. A paging route stripped of its mute must come back flagged, which is
what `test_every_route_a_paging_alert_reaches_is_muted` exists to catch. The `Watchdog` route
never takes the mute, so walking the `Watchdog` set must come back flagged too.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing.py
"""

import re

import pytest
from lib import yaml_fast
from _k8s_render import render_role_template

MUTE = "weekly-reboot"
# Only the equality matcher appears in the tree. Any other operator raises in `_matches`
# rather than reading as no match, which would silently skip a route.
EQUALITY = re.compile(r'^(\w+)="([^"]*)"$')

# Each label set and the receivers Alertmanager sends it to.
PAGING = {
    "warning": (
        {
            "alertname": "K8sDeploymentReplicasUnavailable",
            "severity": "warning",
            "service": "valheim",
        },
        ["discord"],
    ),
    "critical": (
        {
            "alertname": "K8sDeploymentReplicasUnavailable",
            "severity": "critical",
            "service": "valheim",
        },
        ["discord", "email"],
    ),
}
WATCHDOG = {"alertname": "Watchdog", "severity": "info", "service": "observability"}


def route_tree() -> dict:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    return yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])["route"]


def _matches(matcher: str, labels: dict[str, str]) -> bool:
    found = EQUALITY.match(matcher)
    if found is None:
        raise AssertionError(f"the walk cannot read matcher {matcher!r}; teach it")
    name, value = found.groups()
    return labels.get(name, "") == value


def reached(route: dict, labels: dict[str, str]) -> list[dict]:
    """The routes `labels` ends on below `route`, which the caller has already matched."""
    found = []
    for child in route.get("routes", []):
        if all(_matches(m, labels) for m in child.get("matchers", [])):
            found.extend(reached(child, labels))
            if not child.get("continue", False):
                break
    return found or [route]


def unmuted(labels: dict[str, str], tree: dict | None = None) -> list[str]:
    """The receivers of the routes `labels` reaches in `tree`, the rendered one by default,
    that do not carry the mute."""
    return [
        route["receiver"]
        for route in reached(tree or route_tree(), labels)
        if MUTE not in route.get("mute_time_intervals", [])
    ]


@pytest.mark.parametrize("severity", sorted(PAGING))
def test_every_route_a_paging_alert_reaches_is_muted(severity):
    # fact: ansible/roles/k8s/observability/CLAUDE.md#Alert plane
    labels, receivers = PAGING[severity]
    assert [r["receiver"] for r in reached(route_tree(), labels)] == receivers
    assert unmuted(labels) == []


def test_a_paging_route_without_the_mute_is_flagged():
    tree = route_tree()
    catch_all = tree["routes"][-1]
    assert "matchers" not in catch_all, catch_all
    del catch_all["mute_time_intervals"]
    labels, _ = PAGING["warning"]
    assert unmuted(labels, tree) == ["discord"]


def test_the_watchdog_reaching_an_unmuted_route_is_flagged():
    assert unmuted(WATCHDOG) == ["healthchecks"]


def test_a_matcher_the_walk_cannot_read_is_flagged():
    """Guards the walk's own parser, not the rendered config: a matcher form it cannot read
    must stop the walk rather than skip a route the tests above would then never check."""
    with pytest.raises(AssertionError, match="cannot read"):
        _matches('alertname=~"K8s.*"', {"alertname": "K8sDeploymentZeroAvailable"})
