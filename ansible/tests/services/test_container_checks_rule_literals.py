"""The census floor in container-checks.yaml is the 20 pods the deleted bridge check measured.

`CAdvisorCoverageShortfall` ports monitor-bridge's `_cadvisor_blind` census. M6's batch A2
deleted that check, and its `CADVISOR_PODS_MIN` env key went with it, so the floor lives in the
rule alone and this test pins it as a literal.

DECIDED: floor 20 pods, from measurement rather than intuition. Over the 7d to 2026-08-27 the
pre-filter counts never fell below 98 (restarts), 98 (oom) and 70 (cpu, which sees only pods
carrying a cpu limit); live values were 99/99/71. The cpu arm counts the DIVISION, not its
denominator: a PromQL binary op drops series present on only one side, so the two can differ.
They happened to agree here (both 71 live, 70 at the trough), which is why the easier query is
the wrong one to re-measure from. 20 sits 3.5x under the smallest observed trough, so a deploy
draining pods cannot reach it, while a broken selector or a vanished kubernetes-cadvisor job
lands at 0. One floor for all three arms, not one each: a floor applied to two of three would
reproduce the selector-drift class inside its own fix. Lowering it below about 70 blinds the cpu
arm first.

Run: uv run pytest ansible/tests/services/test_container_checks_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "container-checks.yaml"
RULE_FLOOR = re.compile(r"\) or vector\(0\)\) < (\d+),")
FLOOR = 20


def rule_floors(expr: str) -> list[int]:
    """The floor each arm of the census expression compares its pod count with."""
    return [int(n) for n in RULE_FLOOR.findall(expr)]


def census_expr() -> str:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == "CAdvisorCoverageShortfall"]
    return rule["expr"]


def test_every_census_arm_uses_the_measured_floor():
    # restarts, oom and cpu: three arms, so a dropped arm fails here as well.
    assert rule_floors(census_expr()) == [FLOOR, FLOOR, FLOOR]


def test_a_floor_edited_on_one_arm_is_flagged():
    assert rule_floors(census_expr().replace("< 20,", "< 25,")) != [FLOOR] * 3
