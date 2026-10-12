"""A stopped fan-out batch can start its Discord page, and nothing else (#4602).

`roles/setup/claude_code/tasks/fanout_alert.yml` installs `fanout-alert@.service` and the polkit
rule that lets the batch accounts start it. `scripts/dev/fanout_lib/notify.py` is what starts
it, so each test holds one side of that contract to the other.

Run: uv run pytest ansible/tests/setup/test_claude_fanout_alert.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from fanout_lib.notify import PAGED_STATES, alert_unit
from lib import yaml_fast

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
RULE = "50-claude-fanout-alert.rules.j2"
ACCOUNTS = {
    "sys_user": "fanout-sentinel-operator",
    "claude_code_agents_present": ["claude"],
}


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "fanout_alert.yml").read_text())


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
        "claude-land@3633.service",
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


def test_the_alert_it_admits_is_the_one_the_role_installs() -> None:
    (alert,) = [t for t in tasks() if "ansible.builtin.import_tasks" in t]
    variables = alert["vars"]
    assert variables["alert_unit_instanced"] is True
    assert alert_unit("fanout-1", "failed").startswith(
        f"{variables['alert_unit_name']}-alert@"
    )


def test_switching_it_off_removes_everything_the_install_writes() -> None:
    installs = [
        t for t in tasks() if t.get("when") == "claude_code_fanout_alert_enabled"
    ]
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
    assert "/etc/polkit-1/rules.d/50-claude-fanout-alert.rules" in dests
    (removal,) = [
        t for t in tasks() if t.get("when") == "not claude_code_fanout_alert_enabled"
    ]
    removed = removal["loop"]
    for dest in dests:
        assert any(dest == r or dest.startswith(f"{r}/") for r in removed), dest
    assert removed[0].endswith(".rules"), "the rule goes first"


def test_main_imports_the_fanout_alert_file() -> None:
    main = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    assert any(
        t.get("ansible.builtin.import_tasks") == "fanout_alert.yml" for t in main
    )
