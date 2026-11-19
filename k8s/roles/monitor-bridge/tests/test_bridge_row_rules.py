"""Each check row's rule in bridge-exporter.yaml follows from its row in the check table.

The rules file is hand-written, and this test joins it to `check_table.CHECKS`. For every row:

- exactly one rule selects its `check` label, with no `shadow` label;
- severity follows the `critical` column, and `service` names the thing checked, never the
  bridge (operator ruling 2026-10-23);
- the rule has `for: 10m` exactly when the row is `startup_grace` or one of `REBOOT_ROWS`
  (operator rulings 2026-10-31 and 2026-11-02). An in-body streak (`arr_queue`, `ha_heartbeat`)
  holds one arm that a `for:` would widen to the whole row;
- a row behind a reachability gate carries that gate's `requires` label and shares the gate
  rule's group, so the rendered Alertmanager inhibit rule reaches it;
- a `prometheus` row's rule excludes it while any `node` target is down, the suppression the
  bridge applies through `EXPORTER_DEPENDENT["node"]`;
- `runbook_url` links the page the `runbook` column names. On `alert-runbooks` the anchor is
  the alert name, lowercased.

The rules that read no row (DomainRegistrationExpiring*, MonitorBridgeDown, BridgeCheckStale)
are not checked here.

Run: uv run pytest k8s/roles/monitor-bridge/tests/test_bridge_row_rules.py
"""

import copy
import re
from collections.abc import Callable
from typing import NamedTuple

import pytest
from _k8s_render import render_role_template
from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
DOCS = "https://docs.local.daniel-hunter.com"
GRACE = "startup_grace"
# Two bridge cycles (operator ruling 2026-10-31): a one-cycle blip must not fire.
GRACE_FOR = "10m"
# The three rows whose rules read 0 for two minutes at the weekly reboot on 2026-11-01 07:43 to
# 07:44Z, and so carry `for: 10m` without being `startup_grace` rows (operator ruling 2026-11-02).
# Named, not derived: a fourth row that wants a `for:` has to be added here in a reviewed PR.
REBOOT_ROWS = frozenset({"b2_reachable", "b2_storage", "host_packages"})
EXPORTER_EXCLUSION = 'unless on() (up{job="node"} == 0)'
EQUALITY = re.compile(r'^(\w+)="([^"]*)"$')


class Gate(NamedTuple):
    requires: str
    alert: str


GATES = {
    "wan_reachable": Gate("wan", "WANUnreachable"),
    "b2_reachable": Gate("b2", "B2Unreachable"),
}


def groups() -> dict[str, list[dict]]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return {group["name"]: group["rules"] for group in doc["groups"]}


def selecting(row: str, rule_groups: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    """Each rule that selects `check="<row>"`, with the name of its group."""
    return [
        (name, rule)
        for name, rules in rule_groups.items()
        for rule in rules
        if f'check="{row}"' in rule["expr"]
    ]


def rule_of(row: str, rule_groups: dict[str, list[dict]]) -> dict:
    [(_, rule)] = selecting(row, rule_groups)
    return rule


def want_runbook_url(row, alert: str) -> str | None:
    if not row.runbook:
        return None
    anchor = f"#{alert.lower()}" if row.runbook == "alert-runbooks" else ""
    return f"{DOCS}/{row.runbook}/{anchor}"


def row_problems(rule_groups: dict[str, list[dict]]) -> list[str]:
    """Why a row's rule does not follow the row, or [].

    Args:
        rule_groups: The parsed rules of bridge-exporter.yaml, keyed by group name.
    """
    problems = []
    group_of = {}
    for row in CHECKS:
        found = selecting(row.name, rule_groups)
        if len(found) != 1:
            problems.append(f"{row.name}: {len(found)} rules select it")
            continue
        [(group, rule)] = found
        group_of[row.name] = group
        labels, alert = rule["labels"], rule["alert"]
        severity = "critical" if row.critical else "warning"
        if labels.get("severity") != severity:
            problems.append(
                f"{row.name}: severity {labels.get('severity')}, not {severity}"
            )
        if labels.get("service") in {None, "", "monitor-bridge"}:
            problems.append(f"{row.name}: service must name the thing checked")
        if "shadow" in labels:
            problems.append(f"{row.name}: {alert} carries a shadow label")
        held = row.gate == GRACE or row.name in REBOOT_ROWS
        want_for = GRACE_FOR if held else None
        if rule.get("for") != want_for:
            problems.append(
                f"{row.name}: {alert} has for={rule.get('for')!r}, want {want_for!r}"
            )
        want_requires = GATES[row.gate].requires if row.gate in GATES else None
        if labels.get("requires") != want_requires:
            problems.append(
                f"{row.name}: requires={labels.get('requires')!r}, want {want_requires!r}"
            )
        if row.is_gate and alert != GATES[row.name].alert:
            problems.append(
                f"{row.name}: the gate's rule is {alert}, not {GATES[row.name].alert}"
            )
        if row.gate == "prometheus" and EXPORTER_EXCLUSION not in rule["expr"]:
            problems.append(f"{row.name}: the rule must exclude a down node exporter")
        url, want_url = (
            rule["annotations"].get("runbook_url"),
            want_runbook_url(row, alert),
        )
        if url != want_url:
            problems.append(
                f"{row.name}: runbook_url {url!r}, the runbook column gives {want_url!r}"
            )
    for row in CHECKS:
        if row.gate in GATES and row.name in group_of and row.gate in group_of:
            if group_of[row.name] != group_of[row.gate]:
                problems.append(f"{row.name}: not in its gate {row.gate}'s rule group")
    return problems


def inhibits_of(config: dict, gate: Gate) -> list[dict]:
    return [
        i
        for i in config.get("inhibit_rules", [])
        if f'alertname="{gate.alert}"' in i.get("source_matchers", [])
    ]


def inhibit_problems(config: dict, gate_row: str) -> list[str]:
    """Why the rendered Alertmanager config's inhibit for `gate_row` would not work, or [].

    Args:
        config: The parsed `alertmanager.yml`.
        gate_row: The name of a gate row in `GATES`.
    """
    gate = GATES[gate_row]
    rule_groups = groups()
    rules = [r for rs in rule_groups.values() for r in rs]
    found = inhibits_of(config, gate)
    if len(found) != 1:
        return [f"{len(found)} inhibit rules take {gate.alert} as their source"]
    [inhibit] = found
    if not any(r["alert"] == gate.alert for r in rules):
        return [f"no rule is named {gate.alert}"]
    parsed = [EQUALITY.match(m) for m in inhibit["target_matchers"]]
    labels = {m.group(1): m.group(2) for m in parsed if m}
    if len(labels) != len(parsed):
        return [
            f"a target matcher is not an equality matcher: {inhibit['target_matchers']}"
        ]
    targets = {
        r["alert"]
        for r in rules
        if all(r["labels"].get(k) == v for k, v in labels.items())
    }
    dependents = {
        rule_of(row.name, rule_groups)["alert"]
        for row in CHECKS
        if row.gate == gate_row
    }
    problems = []
    if targets != dependents:
        problems.append(
            f"the target matchers {inhibit['target_matchers']} select {sorted(targets)}, "
            f"not the {gate_row} dependents"
        )
    if inhibit.get("equal"):
        problems.append(
            f"an equal list {inhibit['equal']} would need labels the rules lack"
        )
    return problems


def rendered_alertmanager_text() -> str:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    return secret["stringData"]["alertmanager.yml"]


def test_each_gate_value_holds_the_rows_its_rules_were_placed_for():
    by_gate: dict[str | None, set[str]] = {}
    for row in CHECKS:
        by_gate.setdefault("is_gate" if row.is_gate else row.gate, set()).add(row.name)
    assert by_gate == {
        "is_gate": {"b2_reachable", "wan_reachable"},
        "wan_reachable": {
            "discord",
            "r2_usage",
            "cloudflare_ips_drift",
            "healthchecks_drift",
            "domain_registration",
        },
        "b2_reachable": {"b2_storage"},
        GRACE: {"n8n", "bazarr", "prowlarr_indexers", "scrutiny", "speedtest"},
        "prometheus": {"host_packages"},
        None: {"arr_queue", "etcd_restore_drill", "ha_heartbeat"},
    }


def test_every_rows_rule_follows_its_row():
    # fact: k8s/roles/monitor-bridge/CLAUDE.md#Gates and hysteresis
    assert row_problems(groups()) == []


def _set(key: str, value) -> Callable[[dict], None]:
    def mutate(rule: dict) -> None:
        rule[key] = value

    return mutate


def _label(key: str, value) -> Callable[[dict], None]:
    def mutate(rule: dict) -> None:
        if value is None:
            del rule["labels"][key]
        else:
            rule["labels"][key] = value

    return mutate


def _drop(key: str) -> Callable[[dict], None]:
    return lambda rule: rule.pop(key)


MUTATIONS = {
    "a grace rule loses its for": ("n8n", _drop("for")),
    "a gated rule gains a for": ("discord", _set("for", GRACE_FOR)),
    "reboot row b2_reachable loses its for": ("b2_reachable", _drop("for")),
    "reboot row b2_storage gets a shorter for": ("b2_storage", _set("for", "5m")),
    "reboot row host_packages loses its for": ("host_packages", _drop("for")),
    "an ungated rule gains a for": ("ha_heartbeat", _set("for", "5m")),
    "an ungated rule gains requires": ("arr_queue", _label("requires", "wan")),
    "a dependent loses requires": ("b2_storage", _label("requires", None)),
    "service names the bridge": ("arr_queue", _label("service", "monitor-bridge")),
    "severity ignores critical": ("scrutiny", _label("severity", "warning")),
    "a rule gains shadow": ("bazarr", _label("shadow", "true")),
    "the exporter exclusion is lost": (
        "host_packages",
        lambda rule: rule.update(expr=rule["expr"].replace(EXPORTER_EXCLUSION, "")),
    ),
    "runbook_url links another page": (
        "healthchecks_drift",
        lambda rule: rule["annotations"].update(runbook_url=f"{DOCS}/alert-runbooks/"),
    ),
    "a rule without a runbook column gains a runbook_url": (
        "speedtest",
        lambda rule: rule["annotations"].update(runbook_url=f"{DOCS}/alert-runbooks/"),
    ),
}


@pytest.mark.parametrize("case", sorted(MUTATIONS))
def test_a_rule_that_departs_from_its_row_is_flagged(case: str):
    row, mutate = MUTATIONS[case]
    rule_groups = copy.deepcopy(groups())
    mutate(rule_of(row, rule_groups))
    assert row_problems(rule_groups)


def test_a_dependent_outside_its_gates_group_is_flagged():
    rule_groups = copy.deepcopy(groups())
    [(group, rule)] = selecting("discord", rule_groups)
    rule_groups[group].remove(rule)
    rule_groups["elsewhere"] = [rule]
    assert row_problems(rule_groups)


@pytest.mark.parametrize("gate_row", sorted(GATES))
def test_the_rendered_inhibit_rule_reaches_exactly_the_gates_dependents(gate_row: str):
    assert (
        inhibit_problems(yaml_fast.safe_load(rendered_alertmanager_text()), gate_row)
        == []
    )


@pytest.mark.parametrize("gate_row", sorted(GATES))
def test_an_inhibit_target_that_misses_the_dependents_is_flagged(gate_row: str):
    config = yaml_fast.safe_load(rendered_alertmanager_text())
    [inhibit] = inhibits_of(config, GATES[gate_row])
    inhibit["target_matchers"] = ['requires="nowhere"']
    assert inhibit_problems(config, gate_row)


@pytest.mark.parametrize("gate_row", sorted(GATES))
def test_an_inhibit_whose_source_is_not_a_rule_is_flagged(gate_row: str):
    config = yaml_fast.safe_load(rendered_alertmanager_text())
    [inhibit] = inhibits_of(config, GATES[gate_row])
    inhibit["source_matchers"] = [f'alertname="{GATES[gate_row].alert}s"']
    assert inhibit_problems(config, gate_row)


def test_one_inhibit_rules_key_holds_both_gate_inhibits():
    """A duplicated YAML key keeps one value and drops the other without an error."""
    text = rendered_alertmanager_text()
    assert text.count("\ninhibit_rules:") == 1
    sources = [i["source_matchers"] for i in yaml_fast.safe_load(text)["inhibit_rules"]]
    for gate in GATES.values():
        assert [f'alertname="{gate.alert}"'] in sources
