"""The census floor in container-checks.yaml equals the bridge's CADVISOR_PODS_MIN.

`CAdvisorCoverageShortfall` ports monitor-bridge's `_cadvisor_blind` census, whose floor the
bridge reads from the `CADVISOR_PODS_MIN` env var its pod receives. Until M6's batch A2 cuts
over, both pagers run, and a floor edited on one side would make them disagree about a blind
cAdvisor.

Run: uv run pytest ansible/tests/services/test_container_checks_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import render_pod_env
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "container-checks.yaml"
RULE_FLOOR = re.compile(r"\) or vector\(0\)\) < (\d+),")


def rule_floors(expr: str) -> list[int]:
    """The floor each arm of the census expression compares its pod count with."""
    return [int(n) for n in RULE_FLOOR.findall(expr)]


def census_expr() -> str:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == "CAdvisorCoverageShortfall"]
    return rule["expr"]


def test_every_census_arm_uses_the_bridge_floor():
    floor = int(render_pod_env("monitor-bridge")["CADVISOR_PODS_MIN"])
    # restarts, oom and cpu: three arms, so a dropped arm fails here as well.
    assert rule_floors(census_expr()) == [floor, floor, floor]


def test_a_floor_edited_on_one_side_is_flagged():
    floor = int(render_pod_env("monitor-bridge")["CADVISOR_PODS_MIN"])
    assert rule_floors(census_expr().replace("< 20,", "< 25,")) != [floor] * 3
