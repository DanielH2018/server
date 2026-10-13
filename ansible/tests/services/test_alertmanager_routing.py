"""Every alert set reaches the routes its expected outcome names: `muted` or `blackhole`.

`test_alertmanager_reboot_mute.py` proves the interval covers the reboot. This module proves
the alerts reach it: it walks the rendered routing tree of `alertmanager.yml` the way
Alertmanager does, children in order, stopping at the first match unless `continue: true`,
and reads each route the walk ends on. Each label set carries one expected outcome:

- `muted` (`PAGING`): every route the set reaches carries the `weekly-reboot` mute. The
  k8s-workloads sets stand for those alerts once they drop `shadow`, and M4's
  `IngressProbesMissing` pages from the start.
- `blackhole` (`SHADOW`): every route the set reaches has the `blackhole` receiver. A rule
  that runs in parallel with a Kuma tile carries `shadow="true"`, and this is the proof that
  it pages nobody. M4's two `IngressProbeFailed*` sets move to `PAGING` in its PR 5, and
  `BlackboxExporterDown` moves in M6's batch A3.

One predicate, "blackhole or muted", would pass a meta alert routed to the blackhole, which is
a pager that never pages. Hence one outcome per set, each with a rejecting half. A paging
route stripped of its mute must come back flagged, and so must the `Watchdog` set, whose route
never takes the mute. For `blackhole`, the `IngressProbesMissing` set carries no `shadow`, so
it reaches the paging routes and must come back flagged.

Run: uv run pytest ansible/tests/services/test_alertmanager_routing.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import ROLES
from _k8s_render import render_role_template

MUTE = "weekly-reboot"
BLACKHOLE = "blackhole"
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
    "ingress-probes-missing": (
        {
            "alertname": "IngressProbesMissing",
            "severity": "warning",
            "service": "observability",
        },
        ["discord"],
    ),
}
# The shadow sets, as M4's ingress-probes rules ship them. A per-target alert takes `service`
# from its probe target, so any probed service stands in.
SHADOW = {
    "ingress-probe-failed": {
        "alertname": "IngressProbeFailed",
        "severity": "warning",
        "service": "homepage",
        "shadow": "true",
    },
    "ingress-probe-failed-critical": {
        "alertname": "IngressProbeFailedCritical",
        "severity": "critical",
        "service": "authelia",
        "shadow": "true",
    },
    "blackbox-exporter-down": {
        "alertname": "BlackboxExporterDown",
        "severity": "critical",
        "service": "blackbox-exporter",
        "shadow": "true",
    },
}
INGRESS_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "ingress-probes.yaml"
)
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


def not_blackholed(labels: dict[str, str], tree: dict | None = None) -> list[str]:
    """The receivers of the routes `labels` reaches in `tree`, the rendered one by default,
    other than the blackhole."""
    return [
        route["receiver"]
        for route in reached(tree or route_tree(), labels)
        if route["receiver"] != BLACKHOLE
    ]


@pytest.mark.parametrize("name", sorted(SHADOW))
def test_every_route_a_shadow_alert_reaches_is_the_blackhole(name):
    labels = SHADOW[name]
    assert [r["receiver"] for r in reached(route_tree(), labels)] == [BLACKHOLE]
    assert not_blackholed(labels) == []


def test_a_paging_alert_expected_as_blackhole_is_flagged():
    labels, receivers = PAGING["ingress-probes-missing"]
    assert not_blackholed(labels) == receivers


def test_the_ingress_sets_carry_the_labels_their_rules_ship():
    """The sets above stand for the ingress-probes rules only while they carry the rules'
    routing labels. A rule that drops `shadow` must move its set, and its outcome, with it."""
    sets = {labels["alertname"]: labels for labels, _ in PAGING.values()}
    sets.update({labels["alertname"]: labels for labels in SHADOW.values()})
    [group] = yaml_fast.safe_load(INGRESS_RULES.read_text())["groups"]
    assert len(group["rules"]) == 4
    for rule in group["rules"]:
        labels = sets[rule["alert"]]
        literal = rule["labels"]
        assert labels.get("shadow") == literal.get("shadow"), rule["alert"]
        assert labels["severity"] == literal["severity"], rule["alert"]
        assert labels["service"] == literal.get("service", labels["service"]), rule[
            "alert"
        ]


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
