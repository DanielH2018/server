"""The Prometheus-gated rows' rules in bridge-exporter.yaml follow from the check table's `gate` column.

`host_packages` joined the table with H3.5c, after M6's A6 plan was written, and its `gate` is
`prometheus`. The operator ruled on 2026-10-31 that it gets a rule of its own, and that the rule
keeps the bridge's exporter suppression, so a dead `node` exporter pages once through
`ScrapeTargetDown`. Three things must hold, and the table is the only source for the family:

- every non-gate row whose `gate` is `prometheus` has exactly one rule selecting its `check`
  label, so a second such row, or a row that loses its rule, fails;
- the rule carries no `for:` and no `requires` label, names the thing checked as its `service`,
  takes its severity from the row's `critical` flag and stays `shadow` until A6-7b;
- the rule excludes the row while any `node` target is down, which is the estate-wide
  suppression the bridge applies through `EXPORTER_DEPENDENT["node"]` today.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_bridge_prometheus_gated_rules.py
"""

import copy

from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"
EXPORTER_EXCLUSION = 'unless on() (up{job="node"} == 0)'


def all_rules() -> list[dict]:
    doc = yaml_fast.safe_load(RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def family() -> list:
    return [r for r in CHECKS if r.gate == "prometheus" and not r.is_gate]


def rules_for(row: str, rules: list[dict]) -> list[dict]:
    return [r for r in rules if f'check="{row}"' in r["expr"]]


def problems(rules: list[dict]) -> list[str]:
    """Why a Prometheus-gated row's rule would not follow the table, or [].

    Args:
        rules: Every rule of `bridge-exporter.yaml`.
    """
    found = []
    for row in family():
        selected = rules_for(row.name, rules)
        if len(selected) != 1:
            found.append(f"{row.name}: {len(selected)} rules select it")
            continue
        [rule] = selected
        labels = rule["labels"]
        if "for" in rule or "requires" in labels:
            found.append(f"{row.name}: the rule has no for: and no requires")
        if labels.get("service") in {None, "", "monitor-bridge"}:
            found.append(f"{row.name}: service must name the thing checked")
        if labels.get("severity") != ("critical" if row.critical else "warning"):
            found.append(f"{row.name}: severity does not follow the critical flag")
        if labels.get("shadow") != "true":
            found.append(f"{row.name}: the rule is shadow until A6-7b")
        if EXPORTER_EXCLUSION not in rule["expr"]:
            found.append(f"{row.name}: the rule must exclude a down node exporter")
    return found


def test_the_family_is_host_packages():
    assert [r.name for r in family()] == ["host_packages"]


def test_a_prometheus_gated_row_has_one_quiet_shadow_rule_that_excludes_a_dead_exporter():
    assert problems(all_rules()) == []


def test_a_rule_that_loses_its_exporter_exclusion_or_gains_a_for_is_flagged():
    rules = copy.deepcopy(all_rules())
    [rule] = rules_for("host_packages", rules)
    rule["expr"] = rule["expr"].replace(EXPORTER_EXCLUSION, "")
    assert problems(rules)
    rules = copy.deepcopy(all_rules())
    [rule] = rules_for("host_packages", rules)
    rule["for"] = "10m"
    assert problems(rules)
