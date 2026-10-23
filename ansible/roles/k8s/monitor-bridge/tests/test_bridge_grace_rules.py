"""The startup-grace family's rules in bridge-exporter.yaml follow from the check table's `gate` column.

M6's batch A6-5 ports the `startup_grace` rows to rules. `bridge.streaks.apply_startup_grace`
holds such a row `up` through one extra 300s cycle today, and the exporter writes its gauge
before that hold, so the rule's `for: 5m` carries the grace once. Four things must hold, and the
table is the only source for each:

- every `startup_grace` row but `workload_log_errors` has one rule selecting its `check` label,
  and `workload_log_errors` has none here, because A7's LogQL rule on loki-homelab replaces it;
- a rule has `for: 5m` exactly when its row is `startup_grace`, so a row that loses its grace,
  or a row that gains a `for:` on top of an in-body streak, fails;
- no such rule carries `requires`, since a `startup_grace` row names no gate;
- severity follows the row's `critical` flag, and `swallowed_verdicts` is the warning that
  stays on `service: monitor-bridge` and names no `verify` label.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_grace_rules.py
"""

import copy

from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
GRACE = "startup_grace"
# The grace is (GRACE_CYCLES - 1) x 300s with GRACE_CYCLES at 2.
GRACE_FOR = "5m"
# The one `startup_grace` row whose rule is not in this file (A7, loki-homelab).
LOKI_RULE_ROW = "workload_log_errors"
# The `service` label names the thing checked (operator ruling, 2026-10-23), except for the one
# row whose subject is the bridge's own log read.
BRIDGE_SERVICE_ROW = "swallowed_verdicts"


def all_rules() -> list[dict]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def rules_for(row: str, rules: list[dict]) -> list[dict]:
    return [r for r in rules if f'check="{row}"' in r["expr"]]


def family() -> list:
    return [r for r in CHECKS if r.gate == GRACE and r.name != LOKI_RULE_ROW]


def for_problems(rules: list[dict]) -> list[str]:
    """Why a rule's `for:` disagrees with its row's `gate` column, or [].

    Args:
        rules: The parsed rules of bridge-exporter.yaml.
    """
    problems = []
    for row in CHECKS:
        found = rules_for(row.name, rules)
        for rule in found:
            want = GRACE_FOR if row.gate == GRACE else None
            if rule.get("for") != want:
                problems.append(
                    f"{row.name}: {rule['alert']} has for={rule.get('for')!r}, want {want!r}"
                )
    return problems


def test_the_family_is_the_six_startup_grace_rows_the_exporter_rules_cover():
    assert {r.name for r in family()} == {
        "n8n",
        "bazarr",
        "prowlarr_indexers",
        "scrutiny",
        "speedtest",
        "swallowed_verdicts",
    }


def test_each_family_row_has_one_rule_and_the_loki_row_has_none_here():
    rules = all_rules()
    for row in family():
        assert len(rules_for(row.name, rules)) == 1, row.name
    assert rules_for(LOKI_RULE_ROW, rules) == []


def test_a_rule_has_for_5m_exactly_when_its_row_is_startup_grace():
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


def test_family_rules_name_no_gate_and_stay_in_shadow():
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        assert "requires" not in rule["labels"], row.name
        assert rule["labels"]["shadow"] == "true", row.name


def test_severity_follows_the_rows_critical_flag():
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        assert rule["labels"]["severity"] == (
            "critical" if row.critical else "warning"
        ), row.name


def test_service_names_the_thing_checked_and_swallowed_verdicts_names_the_bridge():
    rules = all_rules()
    for row in family():
        [rule] = rules_for(row.name, rules)
        service = rule["labels"]["service"]
        if row.name == BRIDGE_SERVICE_ROW:
            assert service == "monitor-bridge"
            assert rule["labels"]["severity"] == "warning"
            assert "verify" not in rule["labels"]
        else:
            assert service != "monitor-bridge", row.name
