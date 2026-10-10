#!/usr/bin/env python3
"""The agent user logs in to the homelab-ui MCP server as its own Authelia account.

`roles/setup/claude_code/tasks/agent_homelab_ui.yml` writes the account's username, password
and the LAN domain into a file only `claude` reads, because the agent has no age key to read
them from SOPS. Each test pins one way that credential could end up readable by someone else,
left behind after the user is switched off, or aimed at an account that does not exist.

Run: uv run pytest ansible/tests/setup/test_agent_homelab_ui_login.py
"""

from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

CLAUDE_CODE = ANSIBLE / "roles" / "setup" / "claude_code"
TASKS = CLAUDE_CODE / "tasks"
PASSWORD = "sentinel-agent-ui-password"
DOMAIN = "sentinel.example"


def tasks(name: str) -> list[dict]:
    return yaml_fast.safe_load((TASKS / name).read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def role_defaults(*parts: str) -> dict:
    return yaml_fast.safe_load(
        ANSIBLE.joinpath("roles", *parts, "defaults", "main.yml").read_text()
    )


def test_the_login_is_given_only_to_an_enabled_agent_user_with_its_browser() -> None:
    """daniel-server's agent has no browser, so it holds no Authelia credential."""
    task = named(tasks("main.yml"), "Give the agent user its homelab-ui login")
    assert task["ansible.builtin.import_tasks"] == "agent_homelab_ui.yml"
    assert task["when"] == [
        "claude_code_agent_user_enabled",
        "claude_code_agent_homelab_ui_enabled",
    ]


def test_the_login_file_is_the_agents_alone_and_never_printed() -> None:
    task = named(
        tasks("agent_homelab_ui.yml"), "Give the agent user its homelab-ui login"
    )
    template = task["ansible.builtin.template"]
    assert template["mode"] == "0600"
    assert template["owner"] == "{{ claude_code_agent_user }}"
    assert template["dest"] == "{{ claude_code_homelab_ui_credentials }}"
    assert task["diff"] is False and task["no_log"] is True


def test_the_login_file_sits_in_a_directory_only_the_agent_enters() -> None:
    task = named(
        tasks("agent_homelab_ui.yml"),
        "Create the agent user's homelab-ui config directory",
    )
    assert task["ansible.builtin.file"]["mode"] == "0700"
    assert task["ansible.builtin.file"]["owner"] == "{{ claude_code_agent_user }}"


def test_an_enabled_agent_user_without_its_password_fails_the_apply() -> None:
    task = named(
        tasks("agent_homelab_ui.yml"),
        "Refuse to give the agent user a homelab-ui login without its password",
    )
    assert task["ansible.builtin.assert"]["that"] == [
        "authelia_agent_password | default('') | length > 0",
        "domain | default('') | length > 0",
    ]
    assert task["no_log"] is True


def test_the_file_carries_the_username_password_and_domain() -> None:
    rendered = render_setup_text(
        "claude_code",
        "agent-homelab-ui-credentials.json.j2",
        {"authelia_agent_password": PASSWORD, "domain": DOMAIN},
    )
    assert yaml_fast.safe_load(rendered) == {
        "username": "claude-agent",
        "password": PASSWORD,
        "domain": DOMAIN,
    }


def test_switching_the_agent_user_or_its_browser_off_removes_the_login() -> None:
    task = named(
        tasks("main.yml"),
        "Remove the agent user's homelab-ui login when it or its browser is switched off",
    )
    assert task["ansible.builtin.file"] == {
        "path": "{{ claude_code_homelab_ui_credentials }}",
        "state": "absent",
    }
    assert task["when"] == (
        "not (claude_code_agent_user_enabled and claude_code_agent_homelab_ui_enabled)"
    )


def test_the_login_names_the_account_the_authelia_role_creates() -> None:
    """The two roles cannot read each other's defaults, so each carries the name."""
    authelia = role_defaults("k8s", "authelia")["authelia_k8s_agent_user"]
    assert (
        role_defaults("setup", "claude_code")["claude_code_homelab_ui_username"]
        == authelia
    )
