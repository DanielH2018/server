"""DiskHealthNotReporting's two deadline literals equal the DEADLINE_S disk-health.sh renders.

`files/rules/host-checks.yaml` is plain YAML, so the rule carries the deadline as a literal
twice: the `> 1500` of the stale-file arm and the `[1500s]` window of the absent_over_time arm.
The script renders DEADLINE_S from `k3s_disk_health_cron_minute`, and
`ansible/tests/setup/test_disk_health_deadline.py` holds that derivation. A cron moved to a
slower cadence without the rule would page on every healthy gap, and one moved faster would
leave the rule slower than the dead-man (M1 convention 1).

Run: uv run pytest ansible/tests/services/test_disk_health_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
ALERT = "DiskHealthNotReporting"
TEMPLATE = ("setup", "k3s", "disk-health.sh.j2")
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")


def rendered_deadline(minute: str | None = None) -> int:
    """DEADLINE_S as disk-health.sh renders it, at the role default or at `minute`."""
    overrides = None if minute is None else {"k3s_disk_health_cron_minute": minute}
    body = render_shell_script(*TEMPLATE, overrides=overrides)
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def rule_expr(rules: str) -> str:
    """The expression of the one rule named DiskHealthNotReporting."""
    [group] = yaml_fast.safe_load(rules)["groups"]
    exprs = [r["expr"] for r in group["rules"] if r.get("alert") == ALERT]
    assert len(exprs) == 1, f"host-checks.yaml has {len(exprs)} {ALERT} rules"
    return exprs[0]


def literal_problems(expr: str, deadline: int) -> list[str]:
    """Why the rule's two deadline literals differ from `deadline`, or nothing."""
    stale = STALE_ARM.findall(expr)
    window = WINDOW.findall(expr)
    problems = []
    for name, found in (
        ("the `>` threshold", stale),
        ("the absent_over_time window", window),
    ):
        if len(found) != 1:
            problems.append(f"{ALERT} has {len(found)} of {name}, not one")
        elif int(found[0]) != deadline:
            problems.append(
                f"{name} reads {found[0]}s, and DEADLINE_S renders {deadline}"
            )
    return problems


def test_literal_problems_is_clean_on_the_shipped_rule():
    deadline = rendered_deadline()
    assert deadline == 1500
    assert literal_problems(rule_expr(RULES.read_text()), deadline) == []


def test_literal_problems_is_flagged_on_a_threshold_that_drifted():
    expr = rule_expr(RULES.read_text()).replace("> 1500", "> 1200")
    assert literal_problems(expr, rendered_deadline()) == [
        "the `>` threshold reads 1200s, and DEADLINE_S renders 1500"
    ]


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    expr = rule_expr(RULES.read_text()).replace("[1500s]", "[1200s]")
    assert literal_problems(expr, rendered_deadline()) == [
        "the absent_over_time window reads 1200s, and DEADLINE_S renders 1500"
    ]


def test_literal_problems_is_flagged_when_the_cron_moves_without_the_rule():
    # */30 renders 3900s. Rendering at the override proves the pin follows the variable.
    assert literal_problems(
        rule_expr(RULES.read_text()), rendered_deadline("*/30")
    ) == [
        "the `>` threshold reads 1500s, and DEADLINE_S renders 3900",
        "the absent_over_time window reads 1500s, and DEADLINE_S renders 3900",
    ]


def test_literal_problems_is_flagged_on_an_arm_the_rule_lost():
    expr = re.sub(
        r"absent_over_time\((.*)\[1500s\]\)",
        r"absent(\1)",
        rule_expr(RULES.read_text()),
    )
    assert literal_problems(expr, rendered_deadline()) == [
        f"{ALERT} has 0 of the absent_over_time window, not one"
    ]
