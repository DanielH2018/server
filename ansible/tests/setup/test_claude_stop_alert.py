"""A Claude Code session that ends on an API error can start its own Discord page (#6021).

`roles/setup/claude_code/tasks/stop_alert.yml` installs `claude-stop-alert@.service` and the
polkit rule that lets the session accounts start it. The dotfiles StopFailure hook starts it with
the error cause as the instance, so the rule must admit that alphabet and the message must speak
about a session, not a fan-out batch.

Run: uv run pytest ansible/tests/setup/test_claude_stop_alert.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
RULE = "50-claude-stop-alert.rules.j2"
ACCOUNTS = {
    "sys_user": "stop-sentinel-operator",
    "claude_code_agents_present": ["claude"],
}


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "stop_alert.yml").read_text())


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
        "claude-land@3633.service",
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
    (alert,) = [t for t in tasks() if "ansible.builtin.import_tasks" in t]
    variables = alert["vars"]
    assert variables["alert_unit_instanced"] is True
    assert variables["alert_unit_name"] == "claude-stop"
    message = variables["alert_unit_message"]
    assert "session" in message
    assert "fanout" not in message and "fan-out" not in message


def test_switching_it_off_removes_everything_the_install_writes() -> None:
    installs = [t for t in tasks() if t.get("when") == "claude_code_stop_alert_enabled"]
    dests = set()
    for t in installs:
        if "ansible.builtin.import_tasks" in t:
            dests.add(
                f"/etc/systemd/system/{t['vars']['alert_unit_name']}-alert@.service"
            )
            dests.add(f"{t['vars']['alert_unit_env_dir']}/alert-webhook.env")
            continue
        module = t.get("ansible.builtin.template") or t["ansible.builtin.file"]
        dests.add(module.get("dest") or module["path"])
    assert "/etc/polkit-1/rules.d/50-claude-stop-alert.rules" in dests
    (removal,) = [
        t for t in tasks() if t.get("when") == "not claude_code_stop_alert_enabled"
    ]
    removed = removal["loop"]
    for dest in dests:
        assert any(dest == r or dest.startswith(f"{r}/") for r in removed), dest
    assert removed[0].endswith(".rules"), "the rule goes first"


def test_main_imports_the_stop_alert_file() -> None:
    main = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    assert any(t.get("ansible.builtin.import_tasks") == "stop_alert.yml" for t in main)
