#!/usr/bin/env python3
"""daniel-box's agent logs in to a peer as the peer's own agent user, for fan-out placement.

The claude-code host component gives the key host's agent the key and the ssh config, and
authorizes that key on each peer that switches the login on.
Each test pins one leg. A leg that slips fails no deploy: the login either stops working,
which `fanout.py place read` reports, or works with more reach than the design gives it.

Run: uv run pytest ansible/tests/setup/test_claude_agent_ssh_login.py
"""

from _agent_package import SETUP, agent_setup_script, render_daniel_server_with
from _setup_render import CLAUDE_CODE, render_component_text
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS
from renderer.renderer_lib import host as host_mode

ROLE = CLAUDE_CODE
HOME = "/usr/lib/homelab/claude-agent-home"
PLACE_KEY = "place root root 0644 "
KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLD peers"
)


def vars_of(path) -> dict:
    return yaml_fast.safe_load(path.read_text())


def test_the_key_half_runs_only_on_the_key_host_and_the_login_half_everywhere_else(
    tmp_path,
) -> None:
    """On the key host `claude` holds the key, so it authorizes nothing there."""
    assert vars_of(ALL_VARS)["claude_agent_ssh_key_host"] == "daniel-box"
    key_path = vars_of(ALL_VARS)["claude_agent_ssh_key_path"]
    box = host_mode.render_host("daniel-box")
    server = render_daniel_server_with(
        tmp_path, claude_agent_ssh_login_enabled=True, claude_agent_peer_public_key=KEY
    )
    box_home = box.files[HOME][0].decode()
    assert 'keypair "$peer_key" ' in box_home and f"peer_key={key_path}" in box_home
    assert "peer_key" not in server.files[HOME][0].decode()
    assert PLACE_KEY in server.files[SETUP][0].decode()
    assert PLACE_KEY not in box.files[SETUP][0].decode()


def test_a_key_that_was_not_read_as_one_ed25519_line_is_refused(tmp_path) -> None:
    setup = agent_setup_script(
        tmp_path, claude_agent_ssh_login_enabled=True, claude_agent_peer_public_key=KEY
    )
    assert '"$(wc -l < "$templates/' in setup and '" -ne 1' in setup
    assert "grep -Eq '^restrict ssh-ed25519 '" in setup


def test_the_agent_user_lingers_after_it_exists(tmp_path) -> None:
    """A batch started with `systemd-run --user` over ssh dies at logout without linger."""
    setup = agent_setup_script(tmp_path)
    assert 'loginctl enable-linger "$agent" "$operator"' in setup
    # loginctl refuses a user it cannot look up, so the linger comes after usermod.
    assert setup.index("usermod --groups") < setup.index("loginctl enable-linger")


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


def peer_config(peers: dict[str, bool]) -> str:
    hostvars = {
        name: {"server_ip": f"10.0.0.{n}", "claude_agent_ssh_login_enabled": enabled}
        for n, (name, enabled) in enumerate(peers.items(), start=10)
    }
    return render_component_text(
        "claude-code",
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
