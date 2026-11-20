"""Every alert set reaches the routes its expected outcome names: `muted` or `blackhole`.

`test_alertmanager_reboot_mute.py` proves the interval covers the reboot. This module proves
the alerts reach it: it walks the rendered routing tree of `alertmanager.yml` the way
Alertmanager does, children in order, stopping at the first match unless `continue: true`,
and reads each route the walk ends on. Each label set carries one expected outcome:

- `muted` (`PAGING`): every route the set reaches carries the `weekly-reboot` mute. The
  k8s-workloads sets stand for those alerts once they drop `shadow`. M4's
  `IngressProbesMissing` pages from the start, and its two `IngressProbeFailed*` sets since
  its PR 5. M4's `BlackboxExporterDown` and every rule of M6's batch A3 (the scrape-target and
  log-pipeline rules) since A3's Cutover-b. Every rule of M6's batch A4 (the edge rules) since
  A4's Cutover-b, every rule of batch A2 (the container-checks rules) since A2's Cutover-b, and
  every rule of batch A5 (the storage rules) since A5's Cutover-b, and every host rule of batch
  A1 since A1's Cutover-b. `ROUTED` names every rules file whose rules all page this way, with
  its count of paging rules and its critical alerts, which also mail.
- `blackhole` (`SHADOW`): every route the set reaches has the `blackhole` receiver. A rule
  that has not cut over to paging carries `shadow="true"`, and this is the proof that
  it pages nobody. M3's two `DiskHealth*` sets move in its PR 5,
  and the two `TextfileCollectorError` sets in M6's batch A1. H3's host-plane rules page from
  the start: each `HostReconcileFailed`, and each `HostUpdateStale` once
  `observability_expected_hosts` lists a host and `host-checks.yaml` carries its rule (H3.9c).
  Each node's `WiredLinkDown` warning pages Discord from the start too.
  M5's four log rules and four ruler-watching alerts page from its PR 4b.
  M6's `HealthGateDrill` (`DRILL`) is critical and carries `drill="true"`, so it must reach
  the blackhole ahead of both critical routes.

One predicate, "blackhole or muted", would pass a meta alert routed to the blackhole, which is
a pager that never pages. Hence one outcome per set, each with a rejecting half. A paging
route stripped of its mute must come back flagged, and so must the `Watchdog` set, whose route
never takes the mute. For `blackhole`, the `IngressProbesMissing` set carries no `shadow`, so
it reaches the paging routes and must come back flagged.

Run: uv run pytest tests/services/test_alertmanager_routing.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import K8S_ROLES
from _k8s_render import render_role_template

MUTE = "weekly-reboot"
BLACKHOLE = "blackhole"
# The tree uses the equality and regex matchers. Any other operator raises in `_matches`
# rather than reading as no match, which would silently skip a route.
MATCHER = re.compile(r'^(\w+)(=~|=)"([^"]*)"$')

RULES_DIR = K8S_ROLES / "observability" / "files" / "rules"
INGRESS_RULES = RULES_DIR / "ingress-probes.yaml"
DEPLOY_PIPELINE_RULES = RULES_DIR / "deploy-pipeline.yaml"
LOKI_LOG_RULES = K8S_ROLES / "loki-homelab" / "files" / "rules" / "homelab-logs.yaml"
LOKI_HEALTH_RULES = RULES_DIR / "loki-homelab.yaml"
# A per-target or per-pod alert takes `service` from its series, so a real service stands in
# for it. No route matches on `service`, so the stand-in cannot move a set.
STAND_IN_SERVICE = "homepage"
CRITICAL_RECEIVERS = ["discord", "email"]

# Each rules file whose every rule pages through the muted routes: the file, its count of
# rules without `shadow`, and the alerts among them that are critical, so mail as well as post.
# A rule added, dropped, shadowed or re-severitied fails its row until the row agrees.
ROUTED = [
    (
        "bridge-exporter.yaml",
        23,
        {
            "WANUnreachable",
            "DiscordDeliveryFailing",
            "R2FreeTierHeadroomLow",
            "MonitorBridgeDown",
            "BridgeCheckStale",
            "B2Unreachable",
            "B2FreeTierHeadroomLow",
            "SmartHealthFailing",
            "DomainRegistrationExpiring",
        },
    ),
    ("container-checks.yaml", 4, set()),
    ("edge.yaml", 7, {"CertExpiringSoon"}),
    ("extra-probes.yaml", 5, {"ExtraProbeFailedCritical"}),
    ("gitops-checks.yaml", 4, set()),
    (
        "host-checks.yaml",
        85,
        {
            "DiskHealthFailed",
            "DiskHealthNotReporting",
            "EtcdSnapshotOffboxFailed",
            "EtcdSnapshotOffboxNotReporting",
            "HostReconcileFailed",
            "HostUpdateStale",
            "LonghornBackupHealthFailed",
            "LonghornBackupHealthNotReporting",
        },
    ),
    (
        "hosts.yaml",
        37,
        {
            "HostDiskFull",
            "UPSOnBattery",
            "UPSBatteryLow",
            "UPSRuntimeLow",
            "UPSReplaceBattery",
            "UPSSensorAbsent",
        },
    ),
    ("initial-setup-checks.yaml", 13, set()),
    ("log-pipeline.yaml", 6, set()),
    (
        "other-setup-checks.yaml",
        16,
        {
            "EtcdRestoreDrillFailed",
            "EtcdRestoreDrillNotReporting",
            "UpsSecondaryFailed",
            "UpsSecondaryNotReporting",
        },
    ),
    ("pi-checks.yaml", 8, {"PiSdHealthFailed", "PiSdHealthNotReporting"}),
    (
        "pod-producers.yaml",
        9,
        {
            "CrowdSecAppSecVerifyFailed",
            "CrowdSecJobStale",
            "CrowdSecJobsExporterAbsent",
        },
    ),
    ("pods.yaml", 6, {"AutofixBridgeDown", "AutofixBridgeStale"}),
    ("registry.yaml", 10, set()),
    ("scrape-targets.yaml", 47, set()),
    (
        "storage.yaml",
        11,
        {
            "LonghornVolumeDegraded",
            "LonghornVolumesMissing",
            "PVCFull",
            "PVCBelowFreeFloor",
            "PVCFreeFloorUnmonitored",
            "PVCCoverageShortfall",
            "LonghornSnapshotNearCap",
            "LonghornSnapshotCapUnmonitored",
            "LonghornDiskOverSchedulingFloor",
            "KubeletCSIMountReadOnly",
            "EtcdDBNearQuota",
        },
    ),
]


def ingress_set(alert: str) -> dict[str, str]:
    """The labels `alert` reaches Alertmanager with, read from the shipped ingress-probes rule.

    Reading the rule rather than copying it means a rule that gains or drops `shadow`, or
    changes `severity`, moves its set to another route, and its expected outcome fails.
    """
    [group] = yaml_fast.safe_load(INGRESS_RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == alert]
    return {
        "alertname": alert,
        "service": STAND_IN_SERVICE,
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


def rules_of(name: str) -> list[dict]:
    [group] = yaml_fast.safe_load((RULES_DIR / name).read_text())["groups"]
    return group["rules"]


def rule_sets(name: str, shadow: bool) -> dict[str, dict[str, str]]:
    """The labels each rule of rules file `name` reaches Alertmanager with, keyed
    `<alert>/<service>`, since several rules share an alert name.

    Args:
        name: The rules file under `RULES_DIR`.
        shadow: Whether to return the rules that carry `shadow`, or those that do not.
    """
    return {
        f"{rule['alert']}/{labels['service']}": labels
        for rule in rules_of(name)
        for labels in [
            {"alertname": rule["alert"], "service": STAND_IN_SERVICE, **rule["labels"]}
        ]
        if ("shadow" in rule["labels"]) == shadow
    }


def loki_sets(shadow: bool) -> dict[str, dict[str, str]]:
    """The labels each M5 rule reaches Alertmanager with, keyed by alert name.

    The log rules live in loki-homelab's `homelab-logs.yaml` and the ruler-watching alerts in
    observability's `loki-homelab.yaml`. Every rule carries a literal `service`, so its own
    labels are the whole set, and a changed `shadow` or `severity` moves it to another route.

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
    "ingress-probes-missing": (ingress_set("IngressProbesMissing"), ["discord"]),
    "ingress-probe-failed": (ingress_set("IngressProbeFailed"), ["discord"]),
    "ingress-probe-failed-critical": (
        ingress_set("IngressProbeFailedCritical"),
        ["discord", "email"],
    ),
    # The exporter pager since M6's batch A3 Cutover-b: critical, so both channels.
    "blackbox-exporter-down": (
        ingress_set("BlackboxExporterDown"),
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
    **{
        name: (
            labels,
            CRITICAL_RECEIVERS if labels["severity"] == "critical" else ["discord"],
        )
        for name, labels in loki_sets(shadow=False).items()
    },
    **{
        name: (
            labels,
            CRITICAL_RECEIVERS if labels["severity"] == "critical" else ["discord"],
        )
        for file, _, _ in ROUTED
        for name, labels in rule_sets(file, shadow=False).items()
    },
}
DEPLOY_HOLD, _ = PAGING["deploy-hold-active"]
# The shadow sets, as M3's host-checks rules ship them.
SHADOW = {**rule_sets("host-checks.yaml", shadow=True), **loki_sets(shadow=True)}
WATCHDOG = {"alertname": "Watchdog", "severity": "info", "service": "observability"}


def route_tree() -> dict:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    return yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])["route"]


def _matches(matcher: str, labels: dict[str, str]) -> bool:
    found = MATCHER.match(matcher)
    if found is None:
        raise AssertionError(f"the walk cannot read matcher {matcher!r}; teach it")
    name, op, value = found.groups()
    if op == "=~":
        return re.fullmatch(value, labels.get(name, "")) is not None
    return labels.get(name, "") == value


def reached_paths(route: dict, labels: dict[str, str]) -> list[list[dict]]:
    """Each chain of routes from `route`, which the caller has already matched, down to a
    route `labels` ends on."""
    found = []
    for child in route.get("routes", []):
        if all(_matches(m, labels) for m in child.get("matchers", [])):
            found.extend([route, *path] for path in reached_paths(child, labels))
            if not child.get("continue", False):
                break
    return found or [[route]]


def reached(route: dict, labels: dict[str, str]) -> list[dict]:
    """The routes `labels` ends on below `route`, which the caller has already matched."""
    return [path[-1] for path in reached_paths(route, labels)]


def inherited(key: str, labels: dict[str, str], tree: dict | None = None) -> list:
    """The `key` setting of each route `labels` ends on in `tree`, the rendered one by
    default: the nearest value on the chain from the root, as Alertmanager inherits it."""
    return [
        next(r[key] for r in reversed(path) if key in r)
        for path in reached_paths(tree or route_tree(), labels)
    ]


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
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
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


def test_a_unit_failure_its_hook_already_paged_reaches_only_the_blackhole():
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
    failed = {
        "alertname": "SystemdUnitsFailed",
        "severity": "warning",
        "service": "daniel-box",
        "unit": "homelab-builder.service",
    }
    assert not_blackholed({**failed, "paged_by": "homelab-alert"}) == []
    assert not_blackholed(failed) == ["discord"]


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


def test_rule_sets_names_every_host_check_rule():
    """Non-vacuity for `rule_sets` over host-checks.yaml: every shadow rule of the group, per host for the rules each
    host carries, and the paging host-plane rules, M3's two disk-health rules among
    them since its PR 5 and B9's four AIDE and rkhunter rules per host since its Cutover-1b."""
    hosts = ("daniel-box", "daniel-server", "daniel-pi")
    assert set(rule_sets("host-checks.yaml", shadow=True)) == (
        {f"TextfileCollectorError/{host}" for host in hosts}
        | {"ComposeUpFailed/daniel-pi"}
    )
    # B9's twelve rules page: a `warning`, which posts to Discord alone, the channel the six
    # Kuma tiles notified.
    b9_alerts = (
        "AideCheckFailed AideCheckNotReporting RkhunterScanFailed RkhunterScanNotReporting"
    ).split()
    # daniel-pi schedules no AIDE scan, so it carries no AIDE pair.
    b9_keys = {
        f"{alert}/{host}"
        for alert in b9_alerts
        for host in hosts
        if not (alert.startswith("Aide") and host == "daniel-pi")
    }
    assert (
        set(rule_sets("host-checks.yaml", shadow=False))
        >= {
            "DiskHealthFailed/daniel-box",
            "DiskHealthNotReporting/daniel-box",
        }
        | {
            f"{alert}/{host}"
            for alert in (
                "HostReconcileFailed",
                "HostRebootOverdue",
                "HostCheckOverdue",
            )
            for host in hosts
        }
        | b9_keys
    )
    for key in b9_keys:
        assert PAGING[key][1] == ["discord"]


def test_loki_sets_name_every_m5_rule():
    """Non-vacuity for `loki_sets`: each paging Loki rule, and the rules that stay shadow."""
    assert set(loki_sets(shadow=False)) == {
        "WorkloadLogErrors",
        "HostLogErrors",
        "HostOutOfMemoryKill",
        "HostFilesystemRemountedReadOnly",
        "LokiHostLogsSilent",
        "LokiPodLogsSilent",
        "LokiPiLogsSilent",
        "LokiHomelabMetricsMissing",
        "LokiRulerEvaluationFailing",
        "LokiRulerNotEvaluating",
        "LokiRulerNotificationsFailing",
        "CloudflareDdnsDirectUpdateSilent",
        "CloudflareDdnsProxiedUpdateSilent",
        "HomeAssistantCastHeartbeatStorm",
    }
    # A7's Cutover-b dropped `shadow` from WorkloadLogErrors, B7's Cutover-1b from its two rules.
    assert loki_sets(shadow=True) == {}
    assert "LokiHomelabDown" not in loki_sets(shadow=True) | loki_sets(shadow=False)


def test_the_a7_log_rule_pages_to_discord_and_a_shadowed_one_does_not():
    """WorkloadLogErrors replaced a warning tile, so it posts to Discord and does not mail. With
    `shadow` back, the walk ends on the blackhole, so the expected receivers no longer match."""
    labels, receivers = PAGING["WorkloadLogErrors"]
    assert (labels["severity"], receivers) == ("warning", ["discord"])
    assert "WorkloadLogErrors" not in SHADOW
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]


def row_problems(rules: list[dict], count: int, critical: set[str]) -> list[str]:
    """How `rules` differ from a `ROUTED` row's count of paging rules and critical alerts."""
    paging = [rule for rule in rules if "shadow" not in rule["labels"]]
    got = {r["alert"] for r in paging if r["labels"]["severity"] == "critical"}
    problems = (
        [f"{len(paging)} paging rules, not {count}"] if len(paging) != count else []
    )
    if got != critical:
        problems.append(f"critical {sorted(got)}, not {sorted(critical)}")
    return problems


@pytest.mark.parametrize(
    ("file", "count", "critical"), ROUTED, ids=[r[0] for r in ROUTED]
)
def test_each_routed_file_holds_its_paging_rules_and_critical_set(
    file, count, critical
):
    assert row_problems(rules_of(file), count, critical) == []


def test_a_routed_rule_that_regains_shadow_is_flagged():
    rules = rules_of("pods.yaml")
    [autofix] = [r for r in rules if r["alert"] == "AutofixBridgeDown"]
    autofix["labels"]["shadow"] = "true"
    assert row_problems(rules, 6, {"AutofixBridgeDown", "AutofixBridgeStale"}) == [
        "5 paging rules, not 6",
        "critical ['AutofixBridgeStale'], not ['AutofixBridgeDown', 'AutofixBridgeStale']",
    ]


def test_a_storage_rule_that_regains_shadow_is_flagged():
    """The rejecting half: with `shadow` back, the walk ends on the blackhole, so the receivers
    `PAGING` expects for the rule no longer match."""
    labels, receivers = PAGING["PVCFull/longhorn"]
    assert receivers == ["discord", "email"]
    shadowed = {**labels, "shadow": "true"}
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]


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
        _matches('alertname!~"K8s.*"', {"alertname": "K8sDeploymentZeroAvailable"})
