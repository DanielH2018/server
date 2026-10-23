"""The B2 family's rules in bridge-exporter.yaml follow from the check table's `gate` column.

M6's batch A6-4 ports `b2_reachable` and the one row behind it, `b2_storage`, to rules. Four
things must hold, and the table is the only source for each:

- a rule selects each row by its `check` label, and the dependent's rule carries the `requires`
  label that names its gate, so Alertmanager's inhibit rule reaches it;
- the dependent's rule and the gate's rule sit in one rule group;
- neither has a `for:`, because a `startup_grace` row is the only A6 row that does;
- the rendered inhibit rule's source names a defined rule, and its target matcher selects
  exactly the dependent, with the same `shadow` state as the source. Alertmanager inhibits
  before it routes, so a shadow source would otherwise silence a live target.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_b2_rules.py
"""

import copy
import re

from _k8s_render import render_role_template
from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
# The gate row, the `requires` value its dependents carry, and the alert that is its rule.
GATE = "b2_reachable"
REQUIRES = "b2"
GATE_ALERT = "B2Unreachable"
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


def inhibit_of(config: dict) -> dict:
    """The one inhibit rule that takes the gate's alert as its source."""
    [inhibit] = inhibits_of(config)
    return inhibit


def inhibit_problems(config: dict) -> list[str]:
    """Why the rendered Alertmanager config's B2 inhibit would not work, or [].

    Args:
        config: The parsed `alertmanager.yml`.
    """
    rules = [r for rs in groups().values() for r in rs]
    found = inhibits_of(config)
    if len(found) != 1:
        return [f"{len(found)} inhibit rules take {GATE_ALERT} as their source"]
    [inhibit] = found
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


def test_the_b2_family_is_the_one_row_behind_the_gate():
    assert dependents() == ["b2_storage"]


def test_the_dependent_rule_requires_the_gate_and_shares_its_group_with_no_for():
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


def test_the_rendered_inhibit_rule_reaches_exactly_the_dependent():
    assert inhibit_problems(rendered_config()) == []


def test_an_inhibit_target_that_misses_the_dependent_is_flagged():
    config = copy.deepcopy(rendered_config())
    inhibit_of(config)["target_matchers"] = ['requires="b3"']
    assert inhibit_problems(config)


def test_an_inhibit_whose_source_is_not_a_rule_is_flagged():
    config = copy.deepcopy(rendered_config())
    inhibit_of(config)["source_matchers"] = ['alertname="B2Unreachables"']
    assert inhibit_problems(config)


def test_one_inhibit_rules_key_holds_both_the_wan_and_the_b2_inhibit():
    """A duplicated YAML key keeps one value and drops the other without an error."""
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    text = secret["stringData"]["alertmanager.yml"]
    assert (
        text.count("\n    inhibit_rules:") == 0 or text.count("\ninhibit_rules:") == 1
    )
    sources = [i["source_matchers"] for i in yaml_fast.safe_load(text)["inhibit_rules"]]
    assert ['alertname="WANUnreachable"'] in sources
    assert ['alertname="B2Unreachable"'] in sources
