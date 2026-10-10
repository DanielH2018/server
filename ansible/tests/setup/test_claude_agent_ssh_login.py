#!/usr/bin/env python3
"""daniel-box's agent logs in to a peer as the peer's own agent user, for fan-out placement.

`roles/setup/claude_code/tasks/agent_peers.yml` gives the key host's agent the key and the ssh
config, and `agent_ssh_login.yml` authorizes that key on each peer that switches the login on.
Each test pins one leg. A leg that slips fails no deploy: the login either stops working,
which `fanout_place.py read` reports, or works with more reach than the design gives it.

Run: uv run pytest ansible/tests/setup/test_claude_agent_ssh_login.py
"""

from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
CLAUDE_MAIN = ROLE / "tasks" / "agent.yml"
AGENT_PEERS = ROLE / "tasks" / "agent_peers.yml"
SSH_LOGIN = ROLE / "tasks" / "agent_ssh_login.yml"


def tasks(path) -> list[dict]:
    return yaml_fast.safe_load(path.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def vars_of(path) -> dict:
    return yaml_fast.safe_load(path.read_text())


def test_the_key_half_runs_only_on_the_key_host_and_the_login_half_everywhere_else() -> (
    None
):
    """On the key host `claude` is the one holding the key, so it authorizes nothing there."""
    main = tasks(CLAUDE_MAIN)
    peers = named(main, "Give the agent user an ssh key for the peers and name them")
    assert peers["ansible.builtin.import_tasks"] == "agent_peers.yml"
    assert peers["when"] == [
        "claude_code_agent_user_enabled",
        "claude_code_agent_is_primary",
        "inventory_hostname == claude_agent_ssh_key_host",
    ]
    login = named(
        main,
        "Let the key host's agent log in as this host's agent user, or take it back",
    )
    assert login["ansible.builtin.import_tasks"] == "agent_ssh_login.yml"
    assert login["when"] == [
        "claude_code_agent_user_enabled",
        "claude_code_agent_is_primary",
        "inventory_hostname != claude_agent_ssh_key_host",
    ]
    assert vars_of(ALL_VARS)["claude_agent_ssh_key_host"] == ("daniel-box")


def test_the_key_is_authorized_while_enabled_and_removed_when_not() -> None:
    task_list = tasks(SSH_LOGIN)
    authorize = named(
        task_list, "Authorize the key host agent's peer key for this host's agent user"
    )
    assert authorize["when"] == "claude_agent_ssh_login_enabled"
    copy = authorize["ansible.builtin.copy"]
    assert copy["content"].startswith("restrict ")
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    remove = named(
        task_list,
        "Remove the agent's authorized key when the peer login is switched off",
    )
    assert remove["when"] == "not claude_agent_ssh_login_enabled"
    assert remove["ansible.builtin.file"] == {"path": copy["dest"], "state": "absent"}


def test_a_key_that_was_not_read_as_one_ed25519_line_is_refused() -> None:
    refuse = named(tasks(SSH_LOGIN), "Refuse to authorize a key that was not read")
    that = refuse["ansible.builtin.assert"]["that"]
    assert any("match('^ssh-ed25519 ')" in rule for rule in that)
    assert any("splitlines() | length == 1" in rule for rule in that)


def test_daniel_server_switches_the_agent_and_its_login_on_and_the_default_is_off() -> (
    None
):
    server = vars_of(HOST_VARS / "daniel-server.yml")
    assert server["claude_code_agent_user_enabled"] is True
    assert server["claude_agent_ssh_login_enabled"] is True
    assert vars_of(ALL_VARS)["claude_agent_ssh_login_enabled"] is False
    assert "claude_agent_ssh_login_enabled" not in vars_of(HOST_VARS / "daniel-box.yml")


def test_the_key_path_is_under_the_agent_home_and_is_not_the_signing_key() -> None:
    defaults = vars_of(ROLE / "defaults" / "main.yml")
    key = vars_of(ALL_VARS)["claude_agent_ssh_key_path"]
    assert key.startswith(defaults["claude_code_agent_user_home"] + "/")
    assert key != defaults["claude_code_agent_signing_key"]


def test_the_key_is_generated_once_as_the_agent_and_the_pair_has_its_modes() -> None:
    task_list = tasks(AGENT_PEERS)
    keygen = named(task_list, "Generate the agent's ssh key for the peers")
    argv = keygen["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["runuser", "-u", "{{ claude_code_agent_user }}", "--"]
    assert (
        keygen["ansible.builtin.command"]["creates"]
        == "{{ claude_agent_ssh_key_path }}"
    )
    modes = named(task_list, "Give the peer key pair the home's group and its modes")
    assert {i["path"]: i["mode"] for i in modes["loop"]} == {
        "{{ claude_agent_ssh_key_path }}": "0600",
        "{{ claude_agent_ssh_key_path }}.pub": "0644",
    }


def test_the_agent_user_lingers_while_enabled_and_stops_when_not() -> None:
    """A batch started with `systemd-run --user` over ssh dies at logout without linger."""
    main = tasks(CLAUDE_MAIN)
    on = named(
        main,
        "Keep the agent user's manager alive after logout, so its fan-out batches survive",
    )
    assert on["when"] == "claude_code_agent_user_enabled"
    assert on["ansible.builtin.command"] == (
        "loginctl enable-linger {{ claude_code_agent_user }}"
    )
    # loginctl refuses a user it cannot look up, so the linger comes after the user exists.
    created = named(main, "Create the agent user, its tools and its clone")
    assert main.index(created) < main.index(on)
    off = named(main, "Stop the switched-off agent user's manager lingering")
    assert off["when"] == "not claude_code_agent_user_enabled"
    assert off["args"]["removes"] == on["args"]["creates"]


def peer_config(peers: dict[str, bool]) -> str:
    hostvars = {
        name: {"server_ip": f"10.0.0.{n}", "claude_agent_ssh_login_enabled": enabled}
        for n, (name, enabled) in enumerate(peers.items(), start=10)
    }
    return render_setup_text(
        "claude_code",
        "agent-ssh-config.j2",
        {
            "groups": {"all": list(peers)},
            "hostvars": hostvars,
            "claude_agent_ssh_key_host": "daniel-box",
            "claude_agent_ssh_key_path": "/var/lib/claude/.ssh/peers_ed25519",
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


def test_the_ssh_config_names_each_enabled_peer_with_the_agent_user() -> None:
    got = stanzas(peer_config({"daniel-box": True, "daniel-server": True}))
    assert list(got) == ["daniel-server"], "the key host is not a peer"
    stanza = got["daniel-server"]
    assert stanza["HostName"] == "10.0.0.11"
    assert stanza["User"] == "claude"
    assert stanza["IdentityFile"] == "/var/lib/claude/.ssh/peers_ed25519"
    assert stanza["IdentitiesOnly"] == "yes"


def test_the_ssh_config_drops_a_peer_whose_switch_is_off() -> None:
    got = stanzas(
        peer_config({"daniel-box": False, "daniel-server": False, "daniel-pi": True})
    )
    assert list(got) == ["daniel-pi"]
