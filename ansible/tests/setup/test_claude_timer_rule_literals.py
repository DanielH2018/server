"""The claude_code timers' NotReporting rules carry the deadline their unit renders, as literals.

`files/rules/host-checks.yaml` is plain YAML, so each `Claude<Check>NotReporting` rule names
its deadline twice: the `>` threshold of the stale-file arm and the `[Ns]` window of the
absent_over_time arm. Each unit renders the deadline from its own timer period through
`roles/setup/claude_code/templates/timer-textfile.j2` (#6165). A timer moved to a slower period
without its rule would page on every healthy gap (M1 convention 1).

The rules also name exactly the hosts whose inventory arms each timer. A host that arms one
with no rule writes a file nothing watches, and a rule left on a host that disarmed it pages
forever, so `ARMED_BY` derives the set from the same flags the install tasks gate on.

Run: uv run pytest ansible/tests/setup/test_claude_timer_rule_literals.py
"""

import re

import pytest
from _helpers import (
    ALL_VARS,
    HOST_VARS,
    K8S_ROLES,
    SETUP_ROLES,
    load_defaults,
    load_yaml,
)
from _setup_render import render_setup_text
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
UNIT_DEADLINE = re.compile(r'write_check_textfile (\S+) 1 "\$\(date \+%%s\)" (\d+)')

# Alert -> (the unit template that writes its check, the check label, the period variable).
PRODUCERS = {
    "ClaudeCloneSyncNotReporting": (
        "claude-clone-sync.service.j2",
        "claude-clone-sync",
        "claude_code_agent_clone_sync_interval",
    ),
    "ClaudeDotfilesSyncNotReporting": (
        "claude-dotfiles-sync.service.j2",
        "claude-dotfiles-sync",
        "claude_code_agent_dotfiles_sync_interval",
    ),
    "ClaudeMemorySyncNotReporting": (
        "claude-memory-sync.service.j2",
        "claude-memory-sync",
        "claude_code_memory_sync_interval",
    ),
    "ClaudeTriagePollNotReporting": (
        "claude-triage-poll.service.j2",
        "claude-triage-poll",
        "claude_code_triage_poll_interval",
    ),
    "ClaudeFanoutLanderNotReporting": (
        "claude-fanout-server-lander.service.j2",
        "claude-fanout-server-lander",
        "claude_code_fanout_server_lander_interval",
    ),
}


def rendered_deadline(alert: str, period: str | None = None) -> int:
    """The deadline the alert's unit renders, at the role default or at `period`."""
    template, check, var = PRODUCERS[alert]
    overrides = {} if period is None else {var: period}
    found = dict(
        UNIT_DEADLINE.findall(render_setup_text("claude_code", template, overrides))
    )
    assert check in found, f"{template} no longer writes the {check} textfile"
    return int(found[check])


def rule_exprs(alert: str) -> list[str]:
    """The expression of every rule with this alert name, one per host."""
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    exprs = [r["expr"] for r in group["rules"] if r.get("alert") == alert]
    assert exprs, f"host-checks.yaml has no {alert} rule"
    return exprs


def literal_problems(expr: str, deadline: int) -> list[str]:
    """Why the rule's two deadline literals differ from `deadline`, or nothing."""
    problems = []
    for name, found in (
        ("the `>` threshold", STALE_ARM.findall(expr)),
        ("the absent_over_time window", WINDOW.findall(expr)),
    ):
        if len(found) != 1:
            problems.append(f"{len(found)} of {name}, not one")
        elif int(found[0]) != deadline:
            problems.append(
                f"{name} reads {found[0]}s, and the unit renders {deadline}"
            )
    return problems


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_literal_problems_is_clean_on_the_shipped_rule(alert):
    deadline = rendered_deadline(alert)
    for expr in rule_exprs(alert):
        assert literal_problems(expr, deadline) == []


def test_the_deadlines_are_two_periods_plus_the_run_timeout_plus_300s():
    assert {alert: rendered_deadline(alert) for alert in PRODUCERS} == {
        "ClaudeCloneSyncNotReporting": 2100,
        "ClaudeDotfilesSyncNotReporting": 2100,
        "ClaudeMemorySyncNotReporting": 2100,
        "ClaudeTriagePollNotReporting": 660,
        "ClaudeFanoutLanderNotReporting": 3300,
    }


# Alert -> the flags that must all be true, beside `has_claude_code`, for a host to run the
# timer. Each is the `when:` its unit's install task reads.
ARMED_BY = {
    "ClaudeCloneSyncNotReporting": ("claude_code_agent_user_enabled",),
    "ClaudeDotfilesSyncNotReporting": (
        "claude_code_agent_user_enabled",
        "claude_code_agent_dotfiles",
    ),
    "ClaudeMemorySyncNotReporting": ("claude_code_memory_sync_enabled",),
    "ClaudeTriagePollNotReporting": ("claude_code_triage_enabled",),
    "ClaudeFanoutLanderNotReporting": ("claude_code_fanout_server_lander_enabled",),
}


def armed_hosts(alert: str) -> set[str]:
    """The hosts whose inventory arms the alert's timer: role default < group_vars < host_vars."""
    base = {**load_defaults(SETUP_ROLES / "claude_code"), **load_yaml(ALL_VARS)}
    armed = set()
    for path in sorted(HOST_VARS.glob("*.yml")):
        if path.name.startswith("_"):
            continue
        merged = {**base, **(load_yaml(path) or {})}
        flags = [merged["has_claude_code"], *(merged[f] for f in ARMED_BY[alert])]
        assert all(isinstance(f, bool) for f in flags), (path.name, alert, flags)
        if all(flags):
            armed.add(path.stem)
    return armed


def rule_origins(alert: str) -> set[str]:
    return {re.findall(r'origin="([^"]+)"', e)[0] for e in rule_exprs(alert)}


def origin_problems(alert: str, armed: set[str], origins: set[str]) -> list[str]:
    """Why the alert's rules do not name exactly the hosts that arm its timer, or nothing."""
    problems = [
        f"{alert}: {host} arms the timer, and no rule watches its file"
        for host in sorted(armed - origins)
    ]
    problems += [
        f"{alert}: {host} does not arm the timer, so its rule pages forever; delete it"
        for host in sorted(origins - armed)
    ]
    return problems


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_origin_problems_is_clean_on_the_shipped_rules(alert):
    assert origin_problems(alert, armed_hosts(alert), rule_origins(alert)) == []


def test_the_syncs_are_armed_on_both_agent_hosts():
    """Non-vacuity: the derivation finds the two hosts the agent user runs on."""
    assert armed_hosts("ClaudeCloneSyncNotReporting") == {"daniel-box", "daniel-server"}
    assert armed_hosts("ClaudeTriagePollNotReporting") == {"daniel-box"}


def test_origin_problems_is_flagged_for_an_armed_host_with_no_rule():
    assert origin_problems("X", {"daniel-box", "daniel-server"}, {"daniel-box"}) == [
        "X: daniel-server arms the timer, and no rule watches its file"
    ]


def test_origin_problems_is_flagged_for_a_rule_on_a_disarmed_host():
    assert origin_problems("X", {"daniel-box"}, {"daniel-box", "daniel-server"}) == [
        "X: daniel-server does not arm the timer, so its rule pages forever; delete it"
    ]


@pytest.mark.parametrize("alert", sorted(PRODUCERS))
def test_literal_problems_is_flagged_when_the_period_moves_without_the_rule(alert):
    shipped = rendered_deadline(alert)
    moved = rendered_deadline(alert, "30min")
    assert moved == shipped + 2 * (1800 - _default_period_s(alert))
    [expr, *_] = rule_exprs(alert)
    assert literal_problems(expr, moved) == [
        f"the `>` threshold reads {shipped}s, and the unit renders {moved}",
        f"the absent_over_time window reads {shipped}s, and the unit renders {moved}",
    ]


def _default_period_s(alert: str) -> int:
    _, _, var = PRODUCERS[alert]
    return 60 * int(load_defaults(SETUP_ROLES / "claude_code")[var].removesuffix("min"))


def test_a_period_that_is_not_minutes_is_refused_not_rendered_with_a_guess():
    with pytest.raises(Exception, match="claude_code_timer_period_not_minutes"):
        rendered_deadline("ClaudeMemorySyncNotReporting", "1h")
