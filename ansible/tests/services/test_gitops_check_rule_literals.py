"""The NotReporting rules' deadline literals equal the DEADLINE_S each B3 producer renders.

`files/rules/gitops-checks.yaml` is plain YAML, so each NotReporting rule carries its deadline as
a literal twice: the `>` of the stale-file arm and the `[<n>s]` window of the absent_over_time arm.
The producer renders DEADLINE_S, and a producer moved to a slower cadence without its rule would
page on every healthy gap, while one moved faster would leave the rule slower than the Kuma tile it
shadows (M1 convention 1).

Run: uv run pytest ansible/tests/services/test_gitops_check_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _setup_render import rendered_setup_text
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "gitops-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# Alert prefix -> (template, the `check` label its textfile carries).
PRODUCERS = {
    "GitHubRulesetDrift": (
        ("setup", "gitops_deploy", "github-ruleset-drift.sh.j2"),
        "github-ruleset-drift",
    ),
    "GitHubInteractionLimit": (
        ("setup", "gitops_deploy", "github-interaction-limit.sh.j2"),
        "github-interaction-limit",
    ),
}


def rendered_deadline(template: tuple[str, str, str]) -> int:
    """DEADLINE_S as the producer renders it."""
    body = render_shell_script(*template)
    [deadline] = re.findall(r"^\s*DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def not_reporting_rules(text: str) -> dict[str, str]:
    """Alert prefix -> expr for every NotReporting rule in the group."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {
        rule["alert"].removesuffix("NotReporting"): rule["expr"]
        for rule in group["rules"]
        if rule["alert"].endswith("NotReporting")
    }


def literal_problems(rules: dict[str, str], deadlines: dict[str, int]) -> list[str]:
    """Why a rule's deadline literals differ from its producer's DEADLINE_S, or nothing."""
    problems = []
    for prefix, (_, check) in PRODUCERS.items():
        expr = rules.get(prefix)
        if expr is None:
            problems.append(f"{prefix}NotReporting has no rule")
            continue
        if f'check="{check}"' not in expr:
            problems.append(f"{prefix}NotReporting does not read {check}")
        for name, found in (
            ("the `>` threshold", STALE_ARM.findall(expr)),
            ("the absent_over_time window", WINDOW.findall(expr)),
        ):
            if len(found) != 1:
                problems.append(
                    f"{prefix}NotReporting has {len(found)} of {name}, not one"
                )
            elif int(found[0]) != deadlines[prefix]:
                problems.append(
                    f"{prefix}NotReporting: {name} reads {found[0]}s, "
                    f"and DEADLINE_S renders {deadlines[prefix]}"
                )
    return problems


def shipped_deadlines() -> dict[str, int]:
    return {prefix: rendered_deadline(spec[0]) for prefix, spec in PRODUCERS.items()}


def test_literal_problems_is_clean_on_the_shipped_rules():
    deadlines = shipped_deadlines()
    # Non-vacuity: the pin found both producers and both rules.
    assert set(deadlines) == set(PRODUCERS)
    rules = not_reporting_rules(RULES.read_text())
    assert set(rules) == set(PRODUCERS)
    assert literal_problems(rules, deadlines) == []


def test_literal_problems_is_flagged_on_a_threshold_that_drifted():
    text = RULES.read_text().replace("> 90000", "> 86400")
    problems = literal_problems(not_reporting_rules(text), shipped_deadlines())
    assert len(problems) == 2  # the `>` threshold of each rule
    assert all("reads 86400s" in p for p in problems)


def test_literal_problems_is_flagged_when_a_producer_deadline_moves_without_its_rule():
    deadlines = shipped_deadlines() | {"GitHubInteractionLimit": 3900}
    problems = literal_problems(not_reporting_rules(RULES.read_text()), deadlines)
    assert len(problems) == 2  # two literals on the one rule
    assert all("DEADLINE_S renders 3900" in p for p in problems)


@pytest.mark.parametrize("prefix", sorted(PRODUCERS))
def test_every_producer_names_the_check_label_its_rule_reads(prefix):
    template, check = PRODUCERS[prefix]
    body = render_shell_script(*template)
    assert re.search(rf"write_check_textfile {re.escape(check)} ", body), (
        f"{template[-1]} does not write the {check} textfile its rules read"
    )


# GitOpsTickStale reads the deployer's own textfile, which no DEADLINE_S renders. Its deadline must
# outlast the longest healthy gap between two ticks' writes: one whole tick, which systemd caps at
# the unit's TimeoutStartSec, then one timer interval before the next starts (#6164).
SPAN = re.compile(r"(\d+)(min|s)")


def span_seconds(span: str) -> int:
    """A `<N>min` or `<N>s` systemd span in seconds, the two forms this role writes."""
    [(n, unit)] = SPAN.findall(span)
    return int(n) * (60 if unit == "min" else 1)


def tick_stale_problems(expr: str, longest_gap: int) -> list[str]:
    """Why GitOpsTickStale's two literals disagree or undercut `longest_gap`, or nothing."""
    found = STALE_ARM.findall(expr) + WINDOW.findall(expr)
    if len(found) != 2 or found[0] != found[1]:
        return [f"GitOpsTickStale's two deadline literals read {found}"]
    if int(found[0]) <= longest_gap:
        return [f"GitOpsTickStale's {found[0]}s does not outlast a {longest_gap}s gap"]
    return []


def shipped_longest_gap() -> int:
    unit = rendered_setup_text("gitops_deploy", "gitops-deploy.service.j2")
    timer = rendered_setup_text("gitops_deploy", "gitops-deploy.timer.j2")
    [timeout] = re.findall(r"^TimeoutStartSec=(\S+)$", unit, re.M)
    [interval] = re.findall(r"^OnUnitActiveSec=(\S+)$", timer, re.M)
    return span_seconds(timeout) + span_seconds(interval)


def tick_stale_expr(text: str) -> str:
    [group] = yaml_fast.safe_load(text)["groups"]
    [expr] = [r["expr"] for r in group["rules"] if r["alert"] == "GitOpsTickStale"]
    return expr


def test_the_tick_staleness_deadline_outlasts_the_longest_tick_gap():
    assert shipped_longest_gap() == 4800  # 70min + 10min: non-vacuity for both reads
    assert tick_stale_problems(tick_stale_expr(RULES.read_text()), 4800) == []


def test_a_tick_staleness_deadline_under_the_gap_is_flagged():
    expr = tick_stale_expr(RULES.read_text().replace("5400", "4500"))
    assert tick_stale_problems(expr, shipped_longest_gap()) == [
        "GitOpsTickStale's 4500s does not outlast a 4800s gap"
    ]
