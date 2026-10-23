"""The WAN family's rules in bridge-exporter.yaml follow from the check table's `gate` column.

M6's batch A6-3 ports the four rows that sit behind `wan_reachable`, and the gate row itself,
to rules. Four things must hold, and the table is the only source for each:

- a rule selects each row by its `check` label, and a dependent's rule carries the `requires`
  label that names its gate, so Alertmanager's inhibit rule reaches it;
- the dependents' rules and the gate's rule sit in one rule group;
- none of them has a `for:`, because a `startup_grace` row is the only A6 row that does;
- the rendered inhibit rule's source names a defined rule, and its target matcher selects
  exactly the dependents, with the same `shadow` state as the source. Alertmanager inhibits
  before it routes, so a shadow source would otherwise silence a live target.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_wan_rules.py
"""

import copy
import re

from _k8s_render import render_role_template
from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
# The gate row, the `requires` value its dependents carry, and the alert that is its rule.
GATE = "wan_reachable"
REQUIRES = "wan"
GATE_ALERT = "WANUnreachable"
EQUALITY = re.compile(r'^(\w+)="([^"]*)"$')


def groups() -> dict[str, list[dict]]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return {group["name"]: group["rules"] for group in doc["groups"]}


def rule_for(row: str) -> tuple[str, dict]:
    """The one rule that selects `check="<row>"`, with the name of its group."""
    found = [
        (name, rule)
        for name, rules in groups().items()
        for rule in rules
        if f'check="{row}"' in rule["expr"]
    ]
    assert len(found) == 1, f"{row}: {len(found)} rules select it"
    return found[0]


def dependents() -> list[str]:
    return [row.name for row in CHECKS if row.gate == GATE]


def inhibits_of(config: dict) -> list[dict]:
    """The inhibit rules that take the gate's alert as their source."""
    return [
        i
        for i in config.get("inhibit_rules", [])
        if f'alertname="{GATE_ALERT}"' in i.get("source_matchers", [])
    ]


def inhibit_problems(config: dict) -> list[str]:
    """Why the rendered Alertmanager config's WAN inhibit would not work, or [].

    Args:
        config: The parsed `alertmanager.yml`.
    """
    rules = [r for rs in groups().values() for r in rs]
    inhibits = inhibits_of(config)
    if len(inhibits) != 1:
        return [f"{len(inhibits)} inhibit rules take {GATE_ALERT} as their source"]
    [inhibit] = inhibits
    source = [r for r in rules if r["alert"] == GATE_ALERT]
    if not source:
        return [f"no rule is named {GATE_ALERT}"]
    parsed = [EQUALITY.match(m) for m in inhibit["target_matchers"]]
    labels = {m.group(1): m.group(2) for m in parsed if m}
    if len(labels) != len(parsed):
        return [
            f"a target matcher is not an equality matcher: {inhibit['target_matchers']}"
        ]
    targets = [
        r for r in rules if all(r["labels"].get(k) == v for k, v in labels.items())
    ]
    problems = []
    if {r["alert"] for r in targets} != {rule_for(n)[1]["alert"] for n in dependents()}:
        problems.append(
            f"the target matchers {inhibit['target_matchers']} select "
            f"{sorted(r['alert'] for r in targets)}, not the {GATE} dependents"
        )
    shadows = {r["labels"].get("shadow") for r in source + targets}
    if len(shadows) != 1:
        problems.append("the source and its targets differ in their shadow label")
    if inhibit.get("equal"):
        problems.append(
            f"an equal list {inhibit['equal']} would need labels the rules lack"
        )
    return problems


def rendered_config() -> dict:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    return yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])


def test_the_wan_family_is_the_four_rows_behind_the_gate():
    assert set(dependents()) == {
        "discord",
        "r2_usage",
        "cloudflare_ips_drift",
        "healthchecks_drift",
    }


def test_each_dependent_rule_requires_the_gate_and_shares_its_group_with_no_for():
    gate_group, gate_rule = rule_for(GATE)
    assert gate_rule["alert"] == GATE_ALERT
    assert "requires" not in gate_rule["labels"]
    assert "for" not in gate_rule
    for row in dependents():
        group, rule = rule_for(row)
        assert rule["labels"].get("requires") == REQUIRES, row
        assert group == gate_group, row
        assert "for" not in rule, row


def test_severity_follows_the_rows_critical_flag():
    for row in CHECKS:
        if row.name != GATE and row.gate != GATE:
            continue
        _, rule = rule_for(row.name)
        assert rule["labels"]["severity"] == (
            "critical" if row.critical else "warning"
        ), row.name


def test_the_rendered_inhibit_rule_reaches_exactly_the_dependents():
    assert inhibit_problems(rendered_config()) == []


def test_an_inhibit_target_that_misses_the_dependents_is_flagged():
    config = copy.deepcopy(rendered_config())
    [inhibit] = inhibits_of(config)
    inhibit["target_matchers"] = ['requires="wam"']
    assert inhibit_problems(config)


def test_an_inhibit_whose_source_is_not_a_rule_is_flagged():
    config = copy.deepcopy(rendered_config())
    [inhibit] = inhibits_of(config)
    inhibit["source_matchers"] = ['alertname="WANUnreachables"']
    assert inhibit_problems(config)
