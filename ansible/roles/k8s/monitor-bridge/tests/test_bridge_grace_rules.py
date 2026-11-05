"""The startup-grace family's rules in bridge-exporter.yaml follow from the check table's `gate` column.

M6's batch A6-5 ports the `startup_grace` rows to rules. `bridge.streaks.apply_startup_grace`
holds such a row `up` through one extra 300s cycle today, and the exporter writes its gauge
before that hold, so the rule's `for: 10m` needs two down cycles. Four things must hold, and the
table is the only source for each:

- every `startup_grace` row has one rule selecting its `check` label;
- a rule has `for: 10m` exactly when its row is `startup_grace` or one of the three reboot rows the
  2026-11-02 ruling named, so a row that loses its grace,
  or a row that gains a `for:` on top of an in-body streak, fails;
- no such rule carries `requires`, since a `startup_grace` row names no gate;
- severity follows the row's `critical` flag, and `service` names the thing checked, never the
  bridge.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_grace_rules.py
"""

import copy

from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
GRACE = "startup_grace"
# Two bridge cycles (operator ruling 2026-10-31): a one-cycle blip must not fire.
GRACE_FOR = "10m"
# The three rows whose rules read 0 for two minutes at the weekly reboot on 2026-11-01 07:43 to
# 07:44Z, and so carry `for: 10m` without being `startup_grace` rows (operator ruling 2026-11-02).
# Named, not derived: a fourth row that wants a `for:` has to be added here in a reviewed PR.
REBOOT_ROWS = frozenset({"b2_reachable", "b2_storage", "host_packages"})


def all_rules() -> list[dict]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def rules_for(row: str, rules: list[dict]) -> list[dict]:
    return [r for r in rules if f'check="{row}"' in r["expr"]]


def family() -> list:
    return [r for r in CHECKS if r.gate == GRACE]


def for_problems(rules: list[dict]) -> list[str]:
    """Why a rule's `for:` disagrees with its row's `gate` column, or [].

    Args:
        rules: The parsed rules of bridge-exporter.yaml.
    """
    problems = []
    for row in CHECKS:
        found = rules_for(row.name, rules)
        for rule in found:
            held = row.gate == GRACE or row.name in REBOOT_ROWS
            want = GRACE_FOR if held else None
            if rule.get("for") != want:
                problems.append(
                    f"{row.name}: {rule['alert']} has for={rule.get('for')!r}, want {want!r}"
                )
    return problems


def test_the_family_is_the_five_startup_grace_rows_the_exporter_rules_cover():
    assert {r.name for r in family()} == {
        "n8n",
        "bazarr",
        "prowlarr_indexers",
        "scrutiny",
        "speedtest",
    }


def test_each_family_row_has_one_rule():
    rules = all_rules()
    for row in family():
        assert len(rules_for(row.name, rules)) == 1, row.name


def test_a_rule_has_for_10m_exactly_when_its_row_is_startup_grace():
    assert for_problems(all_rules()) == []


def test_a_family_rule_that_loses_its_for_is_flagged():
    rules = copy.deepcopy(all_rules())
    [rule] = rules_for("n8n", rules)
    del rule["for"]
    assert for_problems(rules)


def test_a_gated_rule_that_gains_a_for_is_flagged():
    rules = copy.deepcopy(all_rules())
    [rule] = rules_for("discord", rules)
    rule["for"] = GRACE_FOR
    assert for_problems(rules)


def test_a_reboot_row_rule_that_loses_its_for_is_flagged():
    for row in sorted(REBOOT_ROWS):
        rules = copy.deepcopy(all_rules())
        [rule] = rules_for(row, rules)
        assert rule["for"] == GRACE_FOR, row
        del rule["for"]
        assert for_problems(rules), row


def test_family_rules_name_no_gate_and_carry_no_shadow():
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        assert "requires" not in rule["labels"], row.name
        assert "shadow" not in rule["labels"], row.name


def test_severity_follows_the_rows_critical_flag():
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        assert rule["labels"]["severity"] == (
            "critical" if row.critical else "warning"
        ), row.name


def test_service_names_the_thing_checked():
    # Operator ruling, 2026-10-23.
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        assert rule["labels"]["service"] != "monitor-bridge", row.name
