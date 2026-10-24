"""Every alert set reaches the routes its expected outcome names: `muted` or `blackhole`.

`test_alertmanager_reboot_mute.py` proves the interval covers the reboot. This module proves
the alerts reach it: it walks the rendered routing tree of `alertmanager.yml` the way
Alertmanager does, children in order, stopping at the first match unless `continue: true`,
and reads each route the walk ends on. Each label set carries one expected outcome:

- `muted` (`PAGING`): every route the set reaches carries the `weekly-reboot` mute. The
  k8s-workloads sets stand for those alerts once they drop `shadow`. M4's
  `IngressProbesMissing` pages from the start, and its two `IngressProbeFailed*` sets since
  its PR 5.
- `blackhole` (`SHADOW`): every route the set reaches has the `blackhole` receiver. A rule
  that runs in parallel with a Kuma tile carries `shadow="true"`, and this is the proof that
  it pages nobody. M4's `BlackboxExporterDown` moves to `PAGING` in M6's batch A3. M3's two `DiskHealth*` sets move in its PR 5,
  and the two `TextfileCollectorError` sets in M6's batch A1. H3's host-plane rules page from
  the start: each `HostReconcileFailed`, and each `HostUpdateStale` once
  `observability_expected_hosts` lists a host and `host-checks.yaml` carries its rule (H3.9c).
  Each node's `WiredLinkDown` warning pages Discord from the start too (#5124).
  M5's four log rules and four ruler-watching alerts page from its PR 4b. Its
  `LokiHomelabDown` stays `blackhole` until M6's batch A3 deletes it.
  M6's `HealthGateDrill` (`DRILL`) is critical and carries `drill="true"`, so it must reach
  the blackhole ahead of both critical routes.

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

INGRESS_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "ingress-probes.yaml"
)
HOST_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "host-checks.yaml"
DEPLOY_PIPELINE_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "deploy-pipeline.yaml"
)
LOKI_LOG_RULES = (
    ROLES / "k8s" / "loki-homelab" / "files" / "rules" / "homelab-logs.yaml"
)
LOKI_HEALTH_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "loki-homelab.yaml"
)
SCRAPE_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "scrape-targets.yaml"
)
LOG_PIPELINE_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "log-pipeline.yaml"
)
CUTOVER_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "kuma-cutover.yaml"
)
BRIDGE_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "bridge-exporter.yaml"
)
HOSTS_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "hosts.yaml"
# A per-target ingress alert takes `service` from its probe target, so a probed service
# stands in for it.
INGRESS_STAND_IN_SERVICE = "homepage"


def ingress_set(alert: str) -> dict[str, str]:
    """The labels `alert` reaches Alertmanager with, read from the shipped ingress-probes rule.

    Reading the rule rather than copying it means a rule that gains or drops `shadow`, or
    changes `severity`, moves its set to another route, and its expected outcome fails.
    """
    [group] = yaml_fast.safe_load(INGRESS_RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == alert]
    return {
        "alertname": alert,
        "service": INGRESS_STAND_IN_SERVICE,
        **rule["labels"],
    }


def deploy_pipeline_set(alert: str) -> dict[str, str]:
    """The labels `alert` reaches Alertmanager with, read from the shipped deploy-pipeline rule.

    Every rule there carries the literal `service="deploy-pipeline"`, so the rule's own labels
    are the whole set, and a changed `severity` or `service` moves it to another route.
    """
    [group] = yaml_fast.safe_load(DEPLOY_PIPELINE_RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == alert]
    return {"alertname": alert, **rule["labels"]}


def host_sets(shadow: bool) -> dict[str, dict[str, str]]:
    """The labels each host-checks rule reaches Alertmanager with, keyed `<alert>/<service>`.

    `TextfileCollectorError` and the host-plane rules are one rule per host, so the alert name
    alone is not a key. Every rule there carries a literal `service`.

    Args:
        shadow: Whether to return the rules that carry `shadow`, or those that do not.
    """
    [group] = yaml_fast.safe_load(HOST_RULES.read_text())["groups"]
    return {
        f"{rule['alert']}/{rule['labels']['service']}": {
            "alertname": rule["alert"],
            **rule["labels"],
        }
        for rule in group["rules"]
        if ("shadow" in rule["labels"]) == shadow
    }


def literal_sets(path, shadow: bool) -> dict[str, dict[str, str]]:
    """The labels each rule of `path` reaches Alertmanager with, keyed `<alert>/<service>`.

    M6's batch A3 files carry a literal `service` on every rule, and several rules share an
    alert name, so the key adds the service.

    Args:
        path: The rule file.
        shadow: Whether to return the rules that carry `shadow`, or those that do not.
    """
    [group] = yaml_fast.safe_load(path.read_text())["groups"]
    return {
        f"{rule['alert']}/{rule['labels']['service']}": {
            "alertname": rule["alert"],
            **rule["labels"],
        }
        for rule in group["rules"]
        if ("shadow" in rule["labels"]) == shadow
    }


def loki_sets(shadow: bool) -> dict[str, dict[str, str]]:
    """The labels each M5 rule reaches Alertmanager with, keyed by alert name.

    The log rules live in loki-homelab's `homelab-logs.yaml` and the ruler-watching alerts in
    observability's `loki-homelab.yaml`. Every rule carries a literal `service`, so the rule's
    own labels are the whole set, and a rule that gains or drops `shadow`, or changes
    `severity`, moves to another route and fails its expected outcome.

    Args:
        shadow: Whether to return the rules that carry `shadow`, or those that do not.
    """
    sets = {}
    for path in (LOKI_LOG_RULES, LOKI_HEALTH_RULES):
        for group in yaml_fast.safe_load(path.read_text())["groups"]:
            for rule in group["rules"]:
                if ("shadow" in rule["labels"]) == shadow:
                    sets[rule["alert"]] = {"alertname": rule["alert"], **rule["labels"]}
    return sets


CONTAINER_RULES = (
    ROLES / "k8s" / "observability" / "files" / "rules" / "container-checks.yaml"
)


def container_sets() -> dict[str, dict[str, str]]:
    """The labels each M6 batch A2 rule reaches Alertmanager with, keyed by alert name.

    A per-pod rule takes `service` from the workload join, so `homepage` stands in for it, and
    the census rule's literal `service` wins. A2's Cutover-b drops `shadow`, which moves a set
    to `PAGING`, and the expected outcome here fails until it does.
    """
    [group] = yaml_fast.safe_load(CONTAINER_RULES.read_text())["groups"]
    return {
        rule["alert"]: {
            "alertname": rule["alert"],
            "service": INGRESS_STAND_IN_SERVICE,
            **rule["labels"],
        }
        for rule in group["rules"]
    }


# Each label set and the receivers Alertmanager sends it to.
PAGING = {
    # M6's no-pager-window guard pages from the start, critical to both channels.
    "check-without-pager": (
        {
            "alertname": "CheckWithoutPager",
            "severity": "critical",
            "service": "uptime-kuma",
        },
        ["discord", "email"],
    ),
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
    "ingress-probes-missing": (ingress_set("IngressProbesMissing"), ["discord"]),
    "ingress-probe-failed": (ingress_set("IngressProbeFailed"), ["discord"]),
    "ingress-probe-failed-critical": (
        ingress_set("IngressProbeFailedCritical"),
        ["discord", "email"],
    ),
    "deploy-hold-active": (deploy_pipeline_set("DeployHoldActive"), ["discord"]),
    "promotion-stalled": (deploy_pipeline_set("PromotionStalled"), ["discord"]),
    "prod-moved-outside-promote": (
        deploy_pipeline_set("ProdMovedOutsidePromote"),
        ["discord", "email"],
    ),
    "deploy-pipeline-exporter-absent": (
        deploy_pipeline_set("DeployPipelineExporterAbsent"),
        ["discord"],
    ),
    # A critical host rule pages both channels; a warning, such as WiredLinkDown, Discord alone.
    **{
        name: (
            labels,
            ["discord", "email"] if labels["severity"] == "critical" else ["discord"],
        )
        for name, labels in host_sets(shadow=False).items()
    },
    **{
        name: (
            labels,
            ["discord", "email"] if labels["severity"] == "critical" else ["discord"],
        )
        for name, labels in loki_sets(shadow=False).items()
    },
}
DEPLOY_HOLD, _ = PAGING["deploy-hold-active"]
# The shadow sets, as M4's ingress-probes rules and M3's host-checks rules ship them.
SHADOW = {
    "blackbox-exporter-down": ingress_set("BlackboxExporterDown"),
    **host_sets(shadow=True),
    **loki_sets(shadow=True),
    **container_sets(),
    **literal_sets(SCRAPE_RULES, shadow=True),
    **literal_sets(LOG_PIPELINE_RULES, shadow=True),
    **literal_sets(BRIDGE_RULES, shadow=True),
    **literal_sets(HOSTS_RULES, shadow=True),
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


# M6's health-gate drill, written out rather than read from health-gate-drill.yaml: B-1e
# deletes that rule and keeps the route, and the route must still hold this set.
DRILL = {
    "alertname": "HealthGateDrill",
    "severity": "critical",
    "drill": "true",
    "service": "littlelink",
}
CRITICAL_RECEIVERS = ["discord", "email"]


def test_a_critical_drill_alert_reaches_only_the_blackhole():
    assert [r["receiver"] for r in reached(route_tree(), DRILL)] == [BLACKHOLE]
    assert not_blackholed(DRILL) == []


def test_the_drill_set_without_its_label_pages():
    labels = {k: v for k, v in DRILL.items() if k != "drill"}
    assert not_blackholed(labels) == CRITICAL_RECEIVERS


def test_a_drill_route_behind_the_critical_routes_is_flagged():
    tree = route_tree()
    [drill] = [r for r in tree["routes"] if r.get("matchers") == ['drill="true"']]
    tree["routes"].remove(drill)
    tree["routes"].append(drill)
    assert not_blackholed(DRILL, tree) == CRITICAL_RECEIVERS


def test_every_ingress_rule_has_an_expected_outcome():
    """A rule added to ingress-probes.yaml without a set here would never be routed."""
    [group] = yaml_fast.safe_load(INGRESS_RULES.read_text())["groups"]
    covered = {labels["alertname"] for labels in SHADOW.values()}
    covered |= {labels["alertname"] for labels, _ in PAGING.values()}
    assert {rule["alert"] for rule in group["rules"]} <= covered


def test_every_deploy_pipeline_rule_has_an_expected_outcome():
    """A rule added to deploy-pipeline.yaml without a set here would never be routed."""
    [group] = yaml_fast.safe_load(DEPLOY_PIPELINE_RULES.read_text())["groups"]
    covered = {labels["alertname"] for labels, _ in PAGING.values()}
    assert {rule["alert"] for rule in group["rules"]} <= covered


def test_host_sets_names_every_host_check_rule():
    """Non-vacuity for `host_sets`: M3's two shadow collector rules, #5672's fan-out idle rule,
    M6's B1 pairs for the five k3s crons with `KumaDown`, and the paging host-plane rules, M3's
    two disk-health rules among them since its PR 5."""
    assert set(host_sets(shadow=True)) == {
        "TextfileCollectorError/daniel-box",
        "TextfileCollectorError/daniel-server",
        "FanoutSlotsIdleWithFreeWork/daniel-box",
        "ArtifactsSyncFailed/daniel-box",
        "ArtifactsSyncNotReporting/daniel-box",
        "OtlpDoorClosed/daniel-box",
        "OtlpDoorNotReporting/daniel-box",
        "KumaDown/uptime-kuma",
        "LonghornBackupHealthFailed/daniel-box",
        "LonghornBackupHealthNotReporting/daniel-box",
        "EtcdSnapshotOffboxFailed/daniel-box",
        "EtcdSnapshotOffboxNotReporting/daniel-box",
        "ManifestPruneCheckFailed/daniel-box",
        "ManifestPruneCheckNotReporting/daniel-box",
        "ReleaseStalenessCheckFailed/daniel-box",
        "ReleaseStalenessCheckNotReporting/daniel-box",
        "LiveDriftCheckFailed/daniel-box",
        "LiveDriftCheckNotReporting/daniel-box",
    }
    assert set(host_sets(shadow=False)) >= {
        "DiskHealthFailed/daniel-box",
        "DiskHealthNotReporting/daniel-box",
        "HostReconcileFailed/daniel-box",
        "HostReconcileFailed/daniel-server",
        "HostReconcileFailed/daniel-pi",
    }


def test_loki_sets_name_every_m5_rule():
    """Non-vacuity for `loki_sets`: the eight M5 alerts that page from PR 4b, the one
    that stays shadow until M6's batch A3, and A7's shadow WorkloadLogErrors."""
    assert set(loki_sets(shadow=False)) == {
        "LokiHostLogsSilent",
        "LokiPodLogsSilent",
        "LokiPiLogsSilent",
        "KumaNotificationDropped",
        "LokiHomelabMetricsMissing",
        "LokiRulerEvaluationFailing",
        "LokiRulerNotEvaluating",
        "LokiRulerNotificationsFailing",
    }
    assert set(loki_sets(shadow=True)) >= {"LokiHomelabDown", "WorkloadLogErrors"}


def test_container_sets_name_every_a2_rule_and_all_are_shadow():
    """Non-vacuity for `container_sets`: A2's four rules, each shadow until its Cutover-b."""
    sets = container_sets()
    assert set(sets) == {
        "ContainerRestartLoop",
        "ContainerOOMKilled",
        "ContainerCPUThrottled",
        "CAdvisorCoverageShortfall",
    }
    assert all(labels["shadow"] == "true" for labels in sets.values())


def test_every_a3_rule_is_shadow_except_the_cutover_guard():
    """Non-vacuity for `literal_sets`: batch A3's scrape and log-pipeline rules all route to the
    blackhole until Cutover-b, and `CheckWithoutPager` is the one that pages."""
    for path in (SCRAPE_RULES, LOG_PIPELINE_RULES):
        assert literal_sets(path, shadow=False) == {}
        assert literal_sets(path, shadow=True)
    assert set(literal_sets(CUTOVER_RULES, shadow=False)) == {
        "CheckWithoutPager/uptime-kuma"
    }
    assert "ScrapeTargetDown/observability" in SHADOW


def test_every_a1_rule_is_shadow_and_names_each_host_check():
    """Non-vacuity for the A1 sets: the hosts rules all route to the blackhole until their
    cutovers, and the sets hold the bridge's four host checks and the Pi's pressure arms."""
    assert literal_sets(HOSTS_RULES, shadow=False) == {}
    names = {key.split("/")[0] for key in literal_sets(HOSTS_RULES, shadow=True)}
    assert names >= {
        "HostDiskFull",
        "HostMemoryHigh",
        "HostTemperatureHot",
        "UPSOnBattery",
        "PiLoadHigh",
    }
    assert "HostDiskFull/daniel-box" in SHADOW


def test_a_paging_route_without_the_mute_is_flagged():
    tree = route_tree()
    catch_all = tree["routes"][-1]
    assert "matchers" not in catch_all, catch_all
    del catch_all["mute_time_intervals"]
    labels, _ = PAGING["warning"]
    assert unmuted(labels, tree) == ["discord"]


def repeat_intervals(labels: dict[str, str], tree: dict | None = None) -> list[str]:
    """The `repeat_interval` of each route `labels` reaches in `tree`, the root's when the route
    sets none. The tree nests one level, so the root is the only parent a route inherits from."""
    tree = tree or route_tree()
    return [
        route.get("repeat_interval", tree["repeat_interval"])
        for route in reached(tree, labels)
    ]


def test_a_standing_deploy_hold_repeats_daily():
    assert repeat_intervals(DEPLOY_HOLD) == ["24h"]


def test_a_deploy_hold_without_its_route_is_flagged():
    tree = route_tree()
    tree["routes"] = [
        r
        for r in tree["routes"]
        if 'alertname="DeployHoldActive"' not in r.get("matchers", [])
    ]
    assert repeat_intervals(DEPLOY_HOLD, tree) == ["6h"]


def test_the_watchdog_reaching_an_unmuted_route_is_flagged():
    assert unmuted(WATCHDOG) == ["healthchecks"]


def test_a_matcher_the_walk_cannot_read_is_flagged():
    """Guards the walk's own parser, not the rendered config: a matcher form it cannot read
    must stop the walk rather than skip a route the tests above would then never check."""
    with pytest.raises(AssertionError, match="cannot read"):
        _matches('alertname=~"K8s.*"', {"alertname": "K8sDeploymentZeroAvailable"})
