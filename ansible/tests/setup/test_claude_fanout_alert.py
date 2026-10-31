"""A stopped fan-out batch can start its Discord page, and nothing else (#4602).

The claude-code host component ships `fanout-alert@.service` and the polkit rule
(`roles/setup/claude_code/templates/50-claude-fanout-alert.rules.j2`) that lets the batch
accounts start it. `scripts/dev/fanout_lib/notify.py` is what starts
it, so each test holds one side of that contract to the other.

Run: uv run pytest ansible/tests/setup/test_claude_fanout_alert.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from fanout_lib.notify import PAGED_STATES, alert_unit
from lib import yaml_fast

COMPONENT = ANSIBLE.parent / "hosts" / "components" / "claude-code" / "component.yml"
RULE = "50-claude-fanout-alert.rules.j2"
ACCOUNTS = {
    "sys_user": "fanout-sentinel-operator",
    "claude_code_agents_present": ["claude"],
}


def rule() -> str:
    return render_setup_text("claude_code", RULE, ACCOUNTS)


def rule_pattern(rendered: str) -> re.Pattern:
    found = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rendered)
    assert found, "the rule no longer tests the unit name"
    return re.compile(found.group(1))


@pytest.mark.parametrize("state", PAGED_STATES)
@pytest.mark.parametrize(
    "unit", ["fanout-4602", "fanout-1345-1386", "fanout-dotfiles-763"]
)
def test_the_rule_admits_every_page_notify_starts(unit, state) -> None:
    assert rule_pattern(rule()).search(alert_unit(unit, state))


@pytest.mark.parametrize(
    "name",
    [
        alert_unit("fanout-4602", "done"),
        alert_unit("fanout-4602", "landed"),
        alert_unit("fanout-4602", "running"),
        "fanout-alert@fanout-4602.service",
        "fanout-alert@.service",
        "fanout-alert@claude-rc:failed.service",
        "claude-rc-alert.service",
        "homelab-claude-land@3633.service",
        alert_unit("fanout-4602", "failed") + ".d",
    ],
)
def test_the_rule_refuses_any_other_unit(name) -> None:
    assert not rule_pattern(rule()).search(name)


def test_the_rule_admits_the_operator_and_each_agent_and_only_the_start_verb() -> None:
    rendered = rule()
    assert (
        '["fanout-sentinel-operator", "claude"].indexOf(subject.user) < 0' in rendered
    )
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_the_alert_it_admits_is_the_one_the_component_ships() -> None:
    shipped = {
        entry["dst"]
        for entry in yaml_fast.safe_load(COMPONENT.read_text())["files"]
        if entry.get("src") == "units/fanout-alert@.service"
    }
    template = alert_unit("fanout-1", "failed").split("@")[0] + "@.service"
    assert shipped == {f"/usr/lib/systemd/system/{template}"}
    assert (COMPONENT.parent / "units" / "fanout-alert@.service").is_file()
