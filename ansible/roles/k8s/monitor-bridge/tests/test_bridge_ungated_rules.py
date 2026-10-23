"""The ungated family's rules in bridge-exporter.yaml follow from the check table's `gate` column.

M6's batch A6-6 ports the rows that name no gate. Four things must hold, and the table is the
only source for each:

- the family is exactly the five rows with no `gate` value that are not gate rows, so a sixth
  ungated row, or a row that gains a gate, fails here until its rule is placed;
- a rule selects each row by its `check` label and carries no `requires` label, because no
  inhibit rule reaches an ungated row;
- no rule has a `for:`, because `startup_grace` is the only A6 gate value that does, and the two
  in-body streaks (`arr_queue`, `ha_heartbeat`) hold one arm that a `for:` would widen to the row;
- `service` names the thing checked, never the producer (operator ruling 2026-10-23).

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_ungated_rules.py
"""

import copy

from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
FAMILY = [
    "arr_queue",
    "gitops_alive",
    "gitops_status",
    "etcd_restore_drill",
    "ha_heartbeat",
]


def all_rules() -> list[dict]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def ungated() -> list[str]:
    return [row.name for row in CHECKS if not row.gate and not row.is_gate]


def rules_for(row: str, rules: list[dict]) -> list[dict]:
    return [r for r in rules if f'check="{row}"' in r["expr"]]


def problems(rules: list[dict]) -> list[str]:
    """Why an ungated row's rule would not follow the table, or [].

    Args:
        rules: Every rule of `bridge-exporter.yaml`.
    """
    found = []
    by_name = {row.name: row for row in CHECKS}
    for name in ungated():
        selected = rules_for(name, rules)
        if len(selected) != 1:
            found.append(f"{name}: {len(selected)} rules select it")
            continue
        [rule] = selected
        labels = rule["labels"]
        if "requires" in labels:
            found.append(f"{name}: an ungated row has no gate to require")
        if "for" in rule:
            found.append(f"{name}: an ungated row's rule has no for:")
        if labels.get("service") in {None, "", "monitor-bridge"}:
            found.append(f"{name}: service must name the thing checked")
        severity = "critical" if by_name[name].critical else "warning"
        if labels.get("severity") != severity:
            found.append(f"{name}: severity {labels.get('severity')}, not {severity}")
    return found


def test_the_ungated_family_is_the_five_named_rows():
    assert ungated() == FAMILY


def test_each_ungated_row_has_one_rule_with_no_requires_and_no_for():
    assert problems(all_rules()) == []


def test_a_requires_label_a_for_or_a_producer_service_is_flagged():
    for key, value in (("requires", "wan"), ("service", "monitor-bridge")):
        rules = copy.deepcopy(all_rules())
        [rule] = rules_for("arr_queue", rules)
        rule["labels"][key] = value
        assert problems(rules), key
    rules = copy.deepcopy(all_rules())
    [rule] = rules_for("ha_heartbeat", rules)
    rule["for"] = "5m"
    assert problems(rules)
