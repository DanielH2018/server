#!/usr/bin/env python3
"""The agent user logs in to the homelab-ui MCP server as its own Authelia account.

daniel-box's host package renders the account's username, password
and the LAN domain into a file only `claude` reads, because the agent has no age key to read
them from SOPS. Each test pins one way that credential could end up readable by someone else,
left behind after the user is switched off, or aimed at an account that does not exist.

Run: uv run pytest ansible/tests/setup/test_agent_homelab_ui_login.py
"""

from _agent_package import agent_setup_script
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

CLAUDE_CODE = ANSIBLE / "roles" / "setup" / "claude_code"
BOX_PACKAGE = ANSIBLE.parent / "hosts" / "daniel-box" / "package.yml"
PASSWORD = "sentinel-agent-ui-password"
DOMAIN = "sentinel.example"


def role_defaults(*parts: str) -> dict:
    return yaml_fast.safe_load(
        ANSIBLE.joinpath("roles", *parts, "defaults", "main.yml").read_text()
    )


def test_the_login_is_given_only_to_an_enabled_agent_user_with_its_browser() -> None:
    """daniel-server's agent has no browser, so it holds no Authelia credential."""
    (output,) = [
        o
        for o in yaml_fast.safe_load(BOX_PACKAGE.read_text())["outputs"]
        if o["path"].endswith("homelab-ui/credentials.json")
    ]
    assert output["when"] == (
        "claude_code_agent_user_enabled and claude_code_agent_homelab_ui_enabled"
    )


def test_the_login_file_is_the_agents_alone_and_never_persisted() -> None:
    (output,) = [
        o
        for o in yaml_fast.safe_load(BOX_PACKAGE.read_text())["outputs"]
        if o["path"].endswith("homelab-ui/credentials.json")
    ]
    assert (output["owner"], output["group"], output["mode"]) == (
        "claude",
        "ubuntu",
        "0600",
    )
    assert output["keys"] == ["authelia_agent_password"]
    assert output["persist"] is False and output["link"] is False


def test_the_login_file_sits_in_a_directory_only_the_agent_lists(tmp_path) -> None:
    """The operator's group traverses to stat the 0600 login for `probe.py applied` (#6484)."""
    for flag, mode in ((True, "0710"), (False, "0700")):
        setup = agent_setup_script(
            tmp_path / str(flag),
            claude_code_agent_homelab_ui_enabled=True,
            claude_code_agent_operator_read=flag,
        )
        assert f'dir {mode} "$home/.config/homelab-ui"' in setup


def test_switching_the_browser_off_removes_the_login_link_and_on_points_it(
    tmp_path,
) -> None:
    link = '"$home/.config/homelab-ui/credentials.json"'
    off = agent_setup_script(
        tmp_path / "off", claude_code_agent_homelab_ui_enabled=False
    )
    on = agent_setup_script(tmp_path / "on", claude_code_agent_homelab_ui_enabled=True)
    assert f"drop {link}" in off and f'point "$ui_login" {link}' not in off
    assert f'point "$ui_login" {link}' in on and f"drop {link}" not in on


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


def test_the_login_names_the_account_the_authelia_role_creates() -> None:
    """The two roles cannot read each other's defaults, so each carries the name."""
    authelia = role_defaults("k8s", "authelia")["authelia_k8s_agent_user"]
    assert (
        role_defaults("setup", "claude_code")["claude_code_homelab_ui_username"]
        == authelia
    )
