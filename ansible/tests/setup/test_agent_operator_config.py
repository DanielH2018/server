#!/usr/bin/env python3
"""The agent user gets a subset of the operator's Claude config, and gives it back.

`roles/setup/claude_code/tasks/agent_operator_config.yml` copies the operator's user
CLAUDE.md, rules, output styles and skills to `claude`, root-owned and with no hooks, and sets
`outputStyle` and the pytest worker cap in the agent's own settings.json. `claude_code_agent_operator_config` drives it
both ways. A leg that slips fails no deploy: the agent still runs, with the wrong instructions
or with a style Claude Code cannot load.

Run: uv run pytest ansible/tests/setup/test_agent_operator_config.py
"""

import base64
import json
import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast

TASKS = ANSIBLE / "roles" / "setup" / "claude_code" / "tasks"
SWITCH = "claude_code_agent_operator_config"
TREES = ["rules", "output-styles", "skills"]


def tasks(name: str) -> list[dict]:
    return yaml_fast.safe_load((TASKS / name).read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def evaluate(expression, variables: dict):
    """An Ansible expression evaluated by Ansible's own Templar, so its filters are real."""
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def operator_tasks() -> list[dict]:
    return tasks("agent_operator_config.yml")


def test_the_subset_is_installed_only_for_an_enabled_agent_user_without_dotfiles() -> (
    None
):
    """chezmoi rewrites settings.json whole, so the subset's settings task would fight it."""
    task = named(
        tasks("agent.yml"),
        "Give the agent a subset of the operator's Claude config, or take it back",
    )
    assert task["ansible.builtin.import_tasks"] == "agent_operator_config.yml"
    assert task["when"] == [
        "claude_code_agent_user_enabled",
        "not claude_code_agent_dotfiles",
    ]


def test_the_true_arm_installs_the_four_pieces_root_owned_and_without_hooks() -> None:
    task_list = operator_tasks()
    copy = named(task_list, "Copy the operator's user CLAUDE.md beside the agent's")
    args = copy["ansible.builtin.copy"]
    assert copy["loop"] == ["CLAUDE.md"] and args["remote_src"] is True
    assert args["src"].endswith("/{{ item }}")
    assert args["dest"].endswith("/.claude/operator/{{ item }}")
    assert (args["owner"], args["group"], args["mode"]) == ("root", "root", "0644")

    rsync = named(task_list, "Copy the operator's rules, output styles and skills")
    assert rsync["loop"] == TREES
    argv = evaluate(
        rsync["ansible.builtin.command"]["argv"],
        {"item": "skills", "ansible_check_mode": False}
        | {
            "claude_code_agent_operator_config_src": "/home/ubuntu/.claude",
            "claude_code_agent_user_home": "/var/lib/claude",
        },
    )
    assert argv[0] == "rsync" and "--delete" in argv
    assert "--chown=root:root" in argv and "--chmod=D0755,F0644,Fa+X" in argv
    # -L dereferences the symlinks chezmoi leaves; trailing slashes copy contents, not the dir.
    assert re.fullmatch(r"-r\w*L\w*", argv[1])
    # rsync applies --chown to an existing directory only when it also preserves owner and group.
    assert "o" in argv[1] and "g" in argv[1]
    assert argv[-2:] == [
        "/home/ubuntu/.claude/skills/",
        "/var/lib/claude/.claude/skills/",
    ]
    assert "--exclude=/synced/" in argv
    assert "--dry-run" not in argv
    assert rsync["become"] is True and rsync["when"] == SWITCH
    assert "hooks" not in json.dumps(task_list)


def test_check_mode_runs_the_copy_as_a_dry_run() -> None:
    rsync = named(
        operator_tasks(), "Copy the operator's rules, output styles and skills"
    )
    argv = evaluate(
        rsync["ansible.builtin.command"]["argv"],
        {"item": "rules", "ansible_check_mode": True}
        | {
            "claude_code_agent_operator_config_src": "/src",
            "claude_code_agent_user_home": "/dst",
        },
    )
    assert "--dry-run" in argv
    assert "--exclude=/synced/" not in argv
    # --dry-run changes nothing, so the task may run in check mode; its output is the answer.
    assert rsync["check_mode"] is False


def test_the_false_arm_removes_what_root_installed_and_only_that() -> None:
    task_list = operator_tasks()
    directory = named(
        task_list, "Create the directory holding the operator's CLAUDE.md"
    )
    state = directory["ansible.builtin.file"]["state"]
    for switch, expected in ((True, "directory"), (False, "absent")):
        assert evaluate(state, {SWITCH: switch}) == expected

    marker = named(
        task_list, "Look for a copy of the operator's config from an earlier apply"
    )
    assert marker["ansible.builtin.stat"]["path"].endswith("/operator/CLAUDE.md")
    remove = named(
        task_list, "Remove the operator's config from the agent when it is switched off"
    )
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert remove["loop"] == TREES
    # Ownership cannot mark the copy: agent_user.yml hands root-owned home paths to the agent.
    assert remove["when"] == [
        f"not {SWITCH}",
        "claude_code_operator_copied.stat.exists",
    ]
    assert "pw_name" not in json.dumps(remove)


def test_the_template_imports_the_copied_file_only_while_the_switch_is_true() -> None:
    copy = named(
        operator_tasks(), "Copy the operator's user CLAUDE.md beside the agent's"
    )
    claude_md = "{{ claude_code_agent_user_home }}/.claude/CLAUDE.md"
    # Claude Code resolves a relative @import against the importing file's directory.
    relative = (
        copy["ansible.builtin.copy"]["dest"]
        .replace("{{ item }}", "CLAUDE.md")
        .removeprefix(claude_md.removesuffix("CLAUDE.md"))
    )
    assert relative == "operator/CLAUDE.md"

    on = render_setup_text("claude_code", "agent-user-claude-md.j2", {SWITCH: True})
    off = render_setup_text("claude_code", "agent-user-claude-md.j2", {SWITCH: False})
    assert re.findall(r"^@\S+$", on, re.M) == [f"@{relative}"]
    assert "operator/CLAUDE.md" not in off and not re.search(r"^@", off, re.M)


SETTINGS_TASK = "Set the agent's output style, env and pytest worker cap"


def agent_style(
    switch: bool, operator: dict | None, agent: dict | None, caps: bool = False
) -> dict:
    """What the settings task writes (its `_wanted`) given each side's settings.json."""

    def content(data: dict | None):
        return (
            {}
            if data is None
            else {"content": base64.b64encode(json.dumps(data).encode()).decode()}
        )

    task = named(operator_tasks(), SETTINGS_TASK)
    variables = {
        SWITCH: switch,
        "claude_code_login_caps_enabled": caps,
        "claude_code_rc_pytest_workers": 4,
        "claude_code_agent_user": "claude",
        "claude_code_operator_settings": content(operator),
        "claude_code_agent_settings": content(agent),
    }
    for key, expression in task["vars"].items():
        variables[key] = trust_as_template(expression)
    return {
        "wanted": evaluate("{{ _wanted }}", variables),
        "current": evaluate("{{ _current }}", variables),
    }


STYLE = {"outputStyle": "daniel-voice", "hooks": {"x": 1}}


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        # Keys Claude Code wrote survive; the style is added.
        ({"theme": "dark"}, {"theme": "dark", "outputStyle": "daniel-voice"}),
        # No settings.json yet: the file is created holding only the style.
        (None, {"outputStyle": "daniel-voice"}),
        # A style the agent chose is replaced: the subset's job is to give the operator's.
        ({"outputStyle": "mine"}, {"outputStyle": "daniel-voice"}),
    ],
)
def test_the_true_arm_sets_the_operators_style_and_keeps_the_rest(
    agent, expected
) -> None:
    assert agent_style(True, STYLE, agent)["wanted"] == expected


def test_the_style_is_read_from_the_operator_not_hard_coded() -> None:
    wanted = agent_style(True, {"outputStyle": "other"}, {})["wanted"]
    assert wanted == {"outputStyle": "other"}


def test_an_operator_without_a_style_sets_nothing() -> None:
    result = agent_style(True, {"theme": "dark"}, {"theme": "light"})
    assert result["wanted"] == result["current"]


def test_the_false_arm_removes_the_style_only_while_it_is_the_operators() -> None:
    ours = agent_style(False, STYLE, {"theme": "dark", "outputStyle": "daniel-voice"})
    assert ours["wanted"] == {"theme": "dark"}
    theirs = agent_style(False, STYLE, {"outputStyle": "mine"})
    assert theirs["wanted"] == theirs["current"] == {"outputStyle": "mine"}


OPERATOR_ENV = {
    "outputStyle": "daniel-voice",
    "env": {
        "OTEL_METRICS_EXPORTER": "otlp",
        "CLAUDE_ARTIFACTS_HOST": "daniel-box",
        "SUDO_ASKPASS": "/home/ubuntu/.local/bin/tmux-askpass",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "9",
    },
}


def test_the_true_arm_copies_the_operators_env_but_its_home_bound_keys() -> None:
    wanted = agent_style(True, OPERATOR_ENV, {"env": {"MINE": "x"}})["wanted"]
    assert wanted["env"] == {
        "MINE": "x",
        "OTEL_METRICS_EXPORTER": "otlp",
        # The artifacts role mounts the agent's tree under this name.
        "CLAUDE_ARTIFACTS_HOST": "daniel-box-claude",
    }


TELEMETRY = {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}


def test_the_agents_telemetry_is_labelled_with_its_user() -> None:
    wanted = agent_style(True, TELEMETRY, {})["wanted"]
    assert wanted["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "process.owner=claude"
    # An attribute the operator already sets is kept, with the owner appended.
    operator = {"env": TELEMETRY["env"] | {"OTEL_RESOURCE_ATTRIBUTES": "team=a"}}
    wanted = agent_style(True, operator, {})["wanted"]
    assert wanted["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "team=a,process.owner=claude"


def test_no_owner_label_without_the_operators_telemetry() -> None:
    wanted = agent_style(True, {"env": {"EDITOR": "vim"}}, {})["wanted"]
    assert "OTEL_RESOURCE_ATTRIBUTES" not in wanted["env"]
    # The false arm takes the label back with the rest of the copy.
    labelled = {"env": {"OTEL_RESOURCE_ATTRIBUTES": "process.owner=claude"}}
    assert agent_style(False, TELEMETRY, labelled)["wanted"] == {}


def test_the_false_arm_removes_the_operators_env_only_where_it_still_matches() -> None:
    copied = {
        "OTEL_METRICS_EXPORTER": "otlp",
        "CLAUDE_ARTIFACTS_HOST": "daniel-box-claude",
        "MINE": "x",
    }
    wanted = agent_style(False, OPERATOR_ENV, {"env": copied})["wanted"]
    assert wanted == {"env": {"MINE": "x"}}
    changed = agent_style(
        False, OPERATOR_ENV, {"env": {"OTEL_METRICS_EXPORTER": "none"}}
    )
    assert changed["wanted"] == changed["current"]


def test_the_caps_arm_sets_the_pytest_cap_and_keeps_the_other_env_keys() -> None:
    agent = {"env": {"OTHER": "x"}, "theme": "dark"}
    wanted = agent_style(False, STYLE, agent, caps=True)["wanted"]
    assert wanted == {
        "env": {"OTHER": "x", "PYTEST_XDIST_AUTO_NUM_WORKERS": "4"},
        "theme": "dark",
    }
    # No env block yet: one is created holding only the cap.
    assert agent_style(False, STYLE, {}, caps=True)["wanted"] == {
        "env": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "4"}
    }


def test_caps_off_removes_the_pytest_cap_only_while_it_is_the_roles() -> None:
    ours = agent_style(False, STYLE, {"env": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "4"}})
    assert ours["wanted"] == {}
    kept = {"env": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "4", "OTHER": "x"}}
    assert agent_style(False, STYLE, kept)["wanted"] == {"env": {"OTHER": "x"}}
    theirs = agent_style(False, STYLE, {"env": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}})
    assert theirs["wanted"] == theirs["current"]


def test_settings_json_stays_the_agents_and_is_written_only_on_a_change() -> None:
    task = named(operator_tasks(), SETTINGS_TASK)
    args = task["ansible.builtin.copy"]
    assert args["owner"] == "{{ claude_code_agent_user }}"
    assert task["when"] == "_wanted != _current"
    # The file can hold env values, so neither the diff nor the content is printed.
    assert task["diff"] is False and task["no_log"] is True
