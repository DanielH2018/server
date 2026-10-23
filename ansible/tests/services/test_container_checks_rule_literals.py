"""The census floor in container-checks.yaml equals the bridge's CADVISOR_PODS_MIN.

`CAdvisorCoverageShortfall` ports monitor-bridge's `_cadvisor_blind` census, whose floor the
bridge reads from `CADVISOR_PODS_MIN` in `env.yaml.j2`. Until M6's batch A2 cuts over, both
pagers run, and a floor edited on one side would make them disagree about a blind cAdvisor.

Run: uv run pytest ansible/tests/services/test_container_checks_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "container-checks.yaml"
BRIDGE_ENV = K8S_ROLES / "monitor-bridge" / "templates" / "config" / "env.yaml.j2"

BRIDGE_FLOOR = re.compile(r'^CADVISOR_PODS_MIN: "(\d+)"$', re.MULTILINE)
RULE_FLOOR = re.compile(r"\) or vector\(0\)\) < (\d+),")


def bridge_floor(text: str) -> int:
    [found] = BRIDGE_FLOOR.findall(text)
    return int(found)


def rule_floors(expr: str) -> list[int]:
    """The floor each arm of the census expression compares its pod count with."""
    return [int(n) for n in RULE_FLOOR.findall(expr)]


def census_expr() -> str:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == "CAdvisorCoverageShortfall"]
    return rule["expr"]


def test_every_census_arm_uses_the_bridge_floor():
    floor = bridge_floor(BRIDGE_ENV.read_text())
    # restarts, oom and cpu: three arms, so a dropped arm fails here as well.
    assert rule_floors(census_expr()) == [floor, floor, floor]


def test_a_floor_edited_on_one_side_is_flagged():
    edited = BRIDGE_ENV.read_text().replace(
        'CADVISOR_PODS_MIN: "20"', 'CADVISOR_PODS_MIN: "25"'
    )
    assert rule_floors(census_expr()) != [bridge_floor(edited)] * 3
