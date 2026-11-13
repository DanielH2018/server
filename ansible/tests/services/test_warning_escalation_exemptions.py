"""#8218: a warning that fires for 48 hours escalates to a critical, unless it holds by design.

`WarningUnresolved` (`alerting-meta.yaml`) selects firing warnings from `ALERTS`, the series
Prometheus writes for every rule's alerts with the rule's labels. Its promtool test feeds
`ALERTS` directly, so this module holds the real rules' labels to that selector: every paging
warning escalates except the rules that carry `escalate: "false"`. It also walks the rendered
Alertmanager config, so an escalation pages Discord and email, and the HostCheckOverdue
duplicate stays inhibited after it escalates.

Run: uv run pytest ansible/tests/services/test_warning_escalation_exemptions.py
"""

import re

from lib import yaml_fast
from _helpers import ROLES
from test_alertmanager_host_check_inhibit import _matcher, inhibited
from test_alertmanager_routing import reached, route_tree

RULES = ROLES / "k8s" / "observability" / "files" / "rules"
ESCALATION = "WarningUnresolved"
HOLDS_BY_DESIGN = {"DeployHoldActive", "FanoutSlotsIdleWithFreeWork", "FluxSuspended"}
SELECTOR = re.compile(r"ALERTS\{([^}]*)\}")


def alert_rules() -> list[dict]:
    return [
        rule
        for path in sorted(RULES.glob("*.yaml"))
        for group in yaml_fast.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]


def escalation_rule() -> dict:
    [rule] = [r for r in alert_rules() if r["alert"] == ESCALATION]
    return rule


def escalates(rule: dict) -> bool:
    """Whether the ALERTS series of `rule`, firing, matches the escalation's selector."""
    [selector] = SELECTOR.findall(escalation_rule()["expr"])
    series = {"alertname": rule["alert"], "alertstate": "firing", **rule["labels"]}
    return all(_matcher(m.strip(), series) for m in selector.split(","))


def escalated(source: dict[str, str]) -> dict[str, str]:
    """The labels WarningUnresolved fires with for the warning `source`."""
    return {
        **source,
        **escalation_rule()["labels"],
        "alertname": ESCALATION,
        "escalated_from": source["alertname"],
        "escalated_service": source["service"],
    }


def test_every_paging_warning_escalates_but_those_that_hold_by_design():
    warnings = [
        r
        for r in alert_rules()
        if r["labels"]["severity"] == "warning" and "shadow" not in r["labels"]
    ]
    assert {"DocsRefreshFailed", "EvalSweepFailed", "HostCheckOverdue"} <= {
        r["alert"] for r in warnings if escalates(r)
    }
    assert {r["alert"] for r in warnings if not escalates(r)} == HOLDS_BY_DESIGN


def test_shadow_criticals_and_the_escalation_itself_never_escalate():
    rules = alert_rules()
    assert [r["alert"] for r in rules if "shadow" in r["labels"] and escalates(r)] == []
    assert [
        r["alert"]
        for r in rules
        if r["labels"]["severity"] != "warning" and escalates(r)
    ] == []


def test_an_escalation_pages_discord_and_email():
    target = escalated(
        {
            "alertname": "DocsRefreshFailed",
            "severity": "warning",
            "service": "daniel-box",
        }
    )
    assert [r["receiver"] for r in reached(route_tree(), target)] == [
        "discord",
        "email",
    ]


def test_an_escalated_pair_inhibits_the_escalated_overdue_for_its_check():
    pair = {"severity": "warning", "service": "daniel-box", "origin": "daniel-box"}
    source = escalated(
        {"alertname": "AideCheckNotReporting", "check": "aide-check", **pair}
    )
    target = escalated({"alertname": "HostCheckOverdue", "check": "aide-check", **pair})
    other = escalated({"alertname": "HostCheckOverdue", "check": "unpaired", **pair})
    assert inhibited(target, [source, target])
    assert not inhibited(other, [source, other])
