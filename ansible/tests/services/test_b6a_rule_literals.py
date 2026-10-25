"""The two B6a NotReporting rules carry the deadline their producer renders, as literals.

`files/rules/host-checks.yaml` is plain YAML, so `ArtifactsSyncNotReporting` and
`OtlpDoorNotReporting` each carry the deadline twice: the `> N` of the stale-file arm and the
`[Ns]` window of the absent_over_time arm. `sync-artifacts.sh` and `telemetry-health.sh`
render that deadline from their own cron minute. A cron moved to a slower cadence without the
rule would page on every healthy gap (M1 convention 1). The same pin for disk-health is
`test_disk_health_rule_literals.py`.

Run: uv run pytest ansible/tests/services/test_b6a_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _shell_render import render_shell_script, shell_template_path
from jinja2 import StrictUndefined
from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_guard import render_or_error
from validate.shell_templates import template_context

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# (alert, template, deadline variable in the render, cron-minute variable)
PRODUCERS = {
    "ArtifactsSyncNotReporting": (
        "sync-artifacts.sh.j2",
        "deadline_s",
        "claude_code_artifacts_sync_minute",
    ),
    "OtlpDoorNotReporting": (
        "telemetry-health.sh.j2",
        "DEADLINE_S",
        "claude_code_telemetry_health_minute",
    ),
}


def rendered_deadline(alert: str, minute: str | None = None) -> int:
    """The deadline the alert's producer renders, at the role default or at `minute`."""
    template, name, var = PRODUCERS[alert]
    overrides = None if minute is None else {var: minute}
    body = render_shell_script("setup", "claude_code", template, overrides=overrides)
    [deadline] = re.findall(rf"^{name}=(\d+)$", body, re.M)
    return int(deadline)


def rule_expr(alert: str) -> str:
    """The expression of the one rule with this alert name."""
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    exprs = [r["expr"] for r in group["rules"] if r.get("alert") == alert]
    assert len(exprs) == 1, f"host-checks.yaml has {len(exprs)} {alert} rules"
    return exprs[0]


def literal_problems(alert: str, expr: str, deadline: int) -> list[str]:
    """Why the rule's two deadline literals differ from `deadline`, or nothing."""
    problems = []
    for name, found in (
        ("the `>` threshold", STALE_ARM.findall(expr)),
        ("the absent_over_time window", WINDOW.findall(expr)),
    ):
        if len(found) != 1:
            problems.append(f"{alert} has {len(found)} of {name}, not one")
        elif int(found[0]) != deadline:
            problems.append(
                f"{name} reads {found[0]}s, and the producer renders {deadline}"
            )
    return problems


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_literal_problems_is_clean_on_the_shipped_rule(alert):
    assert literal_problems(alert, rule_expr(alert), rendered_deadline(alert)) == []


def test_the_deadlines_are_the_two_periods_plus_300s_the_rules_were_written_for():
    assert rendered_deadline("ArtifactsSyncNotReporting") == 900
    assert rendered_deadline("OtlpDoorNotReporting") == 1500


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_literal_problems_is_flagged_when_the_cron_moves_without_the_rule(alert):
    # */30 renders 3900s. Rendering at the override proves the pin follows the variable.
    deadline = rendered_deadline(alert, "*/30")
    assert deadline == 3900
    assert literal_problems(alert, rule_expr(alert), deadline) == [
        f"the `>` threshold reads {rendered_deadline(alert)}s, and the producer renders 3900",
        f"the absent_over_time window reads {rendered_deadline(alert)}s, and the producer renders 3900",
    ]


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_a_cron_minute_that_is_not_every_n_is_refused_not_rendered_with_a_guess(alert):
    # The shared harness renders an undefined name as STUB, so the refusal only shows under
    # StrictUndefined, which is how Ansible renders.
    template, _name, var = PRODUCERS[alert]
    path = shell_template_path("setup", "claude_code", template)
    env = template_env(path.parent, undefined_cls=StrictUndefined)
    _, err = render_or_error(env, path.name, template_context(path, {var: "7,37"}))
    assert err and "_cron_minute_not_every_n" in err, err


def test_literal_problems_is_flagged_on_an_arm_the_rule_lost():
    alert = "OtlpDoorNotReporting"
    expr = re.sub(r"absent_over_time\((.*)\[1500s\]\)", r"absent(\1)", rule_expr(alert))
    assert literal_problems(alert, expr, 1500) == [
        f"{alert} has 0 of the absent_over_time window, not one"
    ]
