#!/usr/bin/env python3
"""The box agent reaches a peer as a `claude` account with no sudo and one group.

`roles/setup/initial_setup/tasks/claude-peer.yml` builds the account on daniel-server and
daniel-pi, and `roles/setup/claude_code/tasks/agent_peers.yml` gives the agent the key and the
ssh config that reach it. Each test pins one leg. A leg that slips fails no deploy: the login
still works, only with more reach than the design gives the agent.

Run: uv run pytest ansible/tests/setup/test_claude_peer_login.py
"""

import json

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

SETUP = ANSIBLE / "roles" / "setup"
PEER_TASKS = SETUP / "initial_setup" / "tasks" / "claude-peer.yml"
INITIAL_MAIN = SETUP / "initial_setup" / "tasks" / "main.yml"
CLAUDE_MAIN = SETUP / "claude_code" / "tasks" / "main.yml"
AGENT_PEERS = SETUP / "claude_code" / "tasks" / "agent_peers.yml"
INVENTORY = ANSIBLE / "inventory"
PEERS = ("daniel-server", "daniel-pi")
# The one group sshd's `AllowGroups` needs, and the only group the account may hold.
ALLOWED_GROUPS = ["ssh-users"]


def tasks(path) -> list[dict]:
    return yaml_fast.safe_load(path.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def vars_of(path) -> dict:
    return yaml_fast.safe_load(path.read_text())


def account_task() -> dict:
    return named(
        tasks(PEER_TASKS),
        "Create the agent's peer account with ssh-users as its one group",
    )


def test_the_account_joins_ssh_users_and_no_other_group() -> None:
    user = account_task()["ansible.builtin.user"]
    assert user["groups"] == ALLOWED_GROUPS
    # `append: false` makes the list exact, so a group added by hand is removed again.
    assert user["append"] is False


def granted_groups(task_list: list[dict]) -> set[str]:
    """Every group a user-module task in `task_list` hands its user."""
    granted: set[str] = set()
    for task in task_list:
        module = task.get("ansible.builtin.user") or {}
        groups = module.get("groups") or []
        granted.update([groups] if isinstance(groups, str) else groups)
    return granted


def test_only_the_allowed_group_is_granted_and_nothing_names_sudo() -> None:
    task_list = tasks(PEER_TASKS)
    assert granted_groups(task_list) == set(ALLOWED_GROUPS)
    assert "sudoers" not in json.dumps(task_list)
    assert account_task()["ansible.builtin.user"]["password_lock"] is True


def test_a_group_beyond_the_allowed_list_is_flagged() -> None:
    granting = [
        {"ansible.builtin.user": {"name": "x", "groups": ["ssh-users", "docker"]}},
        {"ansible.builtin.user": {"name": "y", "groups": "sudo"}},
    ]
    assert granted_groups(granting) == {"ssh-users", "docker", "sudo"}


def test_the_home_is_outside_home_and_owned_by_root() -> None:
    assert (
        vars_of(SETUP / "initial_setup" / "defaults" / "main.yml")[
            "initial_setup_claude_peer_home"
        ]
        == "/var/lib/claude"
    )
    home = named(
        tasks(PEER_TASKS), "Create the peer account's root-owned home and .ssh"
    )
    assert home["ansible.builtin.file"]["owner"] == "root"
    authorize = named(
        tasks(PEER_TASKS), "Authorize the agent's key for the peer account"
    )
    assert authorize["ansible.builtin.copy"]["owner"] == "root"
    assert authorize["ansible.builtin.copy"]["mode"] == "0644"


def test_the_key_is_authorized_while_enabled_and_removed_when_not() -> None:
    task_list = tasks(PEER_TASKS)
    authorize = named(task_list, "Authorize the agent's key for the peer account")
    assert authorize["when"] == "claude_peer_user_enabled"
    assert authorize["ansible.builtin.copy"]["content"].startswith("restrict,pty ")
    remove = named(task_list, "Remove the agent's authorized key from the peer account")
    assert remove["when"] == "not claude_peer_user_enabled"
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert (
        remove["ansible.builtin.file"]["path"]
        == authorize["ansible.builtin.copy"]["dest"]
    )


def test_the_account_is_built_while_enabled_and_expired_when_not() -> None:
    task_list = tasks(PEER_TASKS)
    assert account_task()["when"] == "claude_peer_user_enabled"
    assert account_task()["ansible.builtin.user"]["expires"] == -1
    expire = named(task_list, "Expire the peer account when it is switched off")
    assert expire["when"][0] == "not claude_peer_user_enabled"
    assert expire["ansible.builtin.user"]["expires"] == 86400


def test_every_enabled_arm_task_is_gated_on_the_switch_and_every_other_on_its_negation() -> (
    None
):
    """A task that lost its `when:` would run in the arm it does not belong to."""
    for task in tasks(PEER_TASKS):
        when = task["when"]
        conditions = when if isinstance(when, list) else [when]
        assert conditions[0] in (
            "claude_peer_user_enabled",
            "not claude_peer_user_enabled",
        ), task["name"]


def test_the_peer_tasks_never_run_on_the_key_host() -> None:
    """On daniel-box `claude` is the agent user, and the switched-off arm would expire it."""
    imp = named(
        tasks(INITIAL_MAIN),
        "Give the box agent a no-sudo ssh login on this peer, or take it back",
    )
    assert imp["ansible.builtin.import_tasks"] == "claude-peer.yml"
    assert imp["when"] == "inventory_hostname != claude_peer_key_host"
    all_vars = vars_of(INVENTORY / "group_vars" / "all.yml")
    assert all_vars["claude_peer_key_host"] == "daniel-box"


def test_claude_code_does_not_expire_the_account_a_peer_switch_owns() -> None:
    """daniel-server runs both roles; without this guard they undo each other every apply."""
    expire = named(
        tasks(CLAUDE_MAIN), "Expire the agent user's account when it is switched off"
    )
    assert "not claude_peer_user_enabled" in expire["when"]


@pytest.mark.parametrize("host", PEERS)
def test_each_peer_switches_the_login_on_and_the_default_is_off(host: str) -> None:
    assert vars_of(INVENTORY / "host_vars" / f"{host}.yml")["claude_peer_user_enabled"]
    assert (
        vars_of(INVENTORY / "group_vars" / "all.yml")["claude_peer_user_enabled"]
        is False
    )


def test_the_key_host_does_not_set_the_peer_switch() -> None:
    assert "claude_peer_user_enabled" not in vars_of(
        INVENTORY / "host_vars" / "daniel-box.yml"
    )


def test_the_key_path_is_under_the_agent_home_and_is_not_the_signing_key() -> None:
    defaults = vars_of(SETUP / "claude_code" / "defaults" / "main.yml")
    key = vars_of(INVENTORY / "group_vars" / "all.yml")["claude_peer_key_path"]
    assert key.startswith(defaults["claude_code_agent_user_home"] + "/")
    assert key != defaults["claude_code_agent_signing_key"]


def test_the_key_is_generated_once_as_the_agent_and_the_pair_has_its_modes() -> None:
    task_list = tasks(AGENT_PEERS)
    keygen = named(task_list, "Generate the agent's ssh key for the peers")
    argv = keygen["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["runuser", "-u", "{{ claude_code_agent_user }}", "--"]
    assert keygen["ansible.builtin.command"]["creates"] == "{{ claude_peer_key_path }}"
    modes = named(task_list, "Give the peer key pair the home's group and its modes")
    assert {i["path"]: i["mode"] for i in modes["loop"]} == {
        "{{ claude_peer_key_path }}": "0600",
        "{{ claude_peer_key_path }}.pub": "0644",
    }


def test_the_agent_gets_the_peer_key_only_while_it_is_enabled() -> None:
    imp = named(
        tasks(CLAUDE_MAIN), "Give the agent user an ssh key for the peers and name them"
    )
    assert imp["ansible.builtin.import_tasks"] == "agent_peers.yml"
    assert imp["when"] == "claude_code_agent_user_enabled"


def peer_config(peers: dict[str, bool]) -> str:
    hostvars = {
        name: {"server_ip": f"10.0.0.{n}", "claude_peer_user_enabled": enabled}
        for n, (name, enabled) in enumerate(peers.items(), start=10)
    }
    return render_setup_text(
        "claude_code",
        "agent-ssh-config.j2",
        {
            "groups": {"all": list(peers)},
            "hostvars": hostvars,
            "claude_peer_key_host": "daniel-box",
            "claude_peer_user": "claude",
            "claude_peer_key_path": "/var/lib/claude/.ssh/peers_ed25519",
        },
    )


def stanzas(config: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    current = None
    for line in config.splitlines():
        if line.startswith("Host "):
            current = out.setdefault(line.split()[1], {})
        elif current is not None and line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split(None, 1)
            current[key] = value
    return out


def test_the_ssh_config_names_each_enabled_peer_with_the_peer_user() -> None:
    got = stanzas(
        peer_config({"daniel-box": True, "daniel-server": True, "daniel-pi": True})
    )
    assert sorted(got) == ["daniel-pi", "daniel-server"], "the key host is not a peer"
    assert got["daniel-server"]["HostName"] == "10.0.0.11"
    assert got["daniel-pi"]["HostName"] == "10.0.0.12"
    for stanza in got.values():
        assert stanza["User"] == "claude"
        assert stanza["IdentityFile"] == "/var/lib/claude/.ssh/peers_ed25519"
        assert stanza["IdentitiesOnly"] == "yes"


def test_the_ssh_config_drops_a_peer_whose_switch_is_off() -> None:
    got = stanzas(
        peer_config({"daniel-box": False, "daniel-server": True, "daniel-pi": False})
    )
    assert list(got) == ["daniel-server"]
