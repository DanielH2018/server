"""A Claude Code session that ends on an API error can start its own Discord page (#6021).

The claude-code host component ships `claude-stop-alert@.service` and the polkit rule
(`roles/setup/claude_code/templates/50-claude-stop-alert.rules.j2`) that lets the session
accounts start it. The dotfiles StopFailure hook starts it with
the error cause as the instance, so the rule must admit that alphabet and the message must speak
about a session, not a fan-out batch.

Run: uv run pytest ansible/tests/setup/test_claude_stop_alert.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

COMPONENT = ANSIBLE.parent / "hosts" / "components" / "claude-code" / "component.yml"
RULE = "50-claude-stop-alert.rules.j2"
ACCOUNTS = {
    "sys_user": "stop-sentinel-operator",
    "claude_code_agents_present": ["claude"],
}


def rule() -> str:
    return render_setup_text("claude_code", RULE, ACCOUNTS)


def rule_pattern(rendered: str) -> re.Pattern:
    found = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rendered)
    assert found, "the rule no longer tests the unit name"
    return re.compile(found.group(1))


@pytest.mark.parametrize(
    "cause", ["rate_limit", "server_error", "authentication_failed", "unknown", "x9"]
)
def test_the_rule_admits_a_page_for_every_cause_the_hook_writes(cause) -> None:
    assert rule_pattern(rule()).search(f"claude-stop-alert@{cause}.service")


@pytest.mark.parametrize(
    "name",
    [
        "claude-stop-alert@.service",
        "claude-stop-alert@Rate_Limit.service",
        "claude-stop-alert@rate-limit.service",
        "claude-stop-alert@rate_limit:failed.service",
        "claude-stop-alert@rate_limit.service.d",
        "claude-stop-alert.service",
        "fanout-alert@fanout-4602:failed.service",
        "homelab-claude-land@3633.service",
    ],
)
def test_the_rule_refuses_any_other_unit(name) -> None:
    assert not rule_pattern(rule()).search(name)


def test_the_rule_admits_the_operator_and_each_agent_and_only_the_start_verb() -> None:
    rendered = rule()
    assert '["stop-sentinel-operator", "claude"].indexOf(subject.user) < 0' in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_the_alert_names_a_session_not_a_fanout_batch() -> None:
    unit = (COMPONENT.parent / "units" / "claude-stop-alert@.service").read_text()
    body = "\n".join(line for line in unit.splitlines() if not line.startswith("#"))
    assert "session" in body
    assert "fanout" not in body and "fan-out" not in body
    shipped = [
        entry["dst"]
        for entry in yaml_fast.safe_load(COMPONENT.read_text())["files"]
        if entry.get("src") == "units/claude-stop-alert@.service"
    ]
    assert shipped == ["/usr/lib/systemd/system/claude-stop-alert@.service"]
