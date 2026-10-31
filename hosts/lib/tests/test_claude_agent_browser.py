"""The agent's homelab-ui browser oneshot, run against stub binaries in a temporary home (H7.40).

hosts/components/claude-code ships `claude-agent-browser`, which homelab-claude-agent-browser.service
runs as the agent. Each test renders daniel-box's package with the agent's home moved under
`tmp_path`, lays a stub Node tree, npm and `claude` there, and runs the script. The Node tree is
already unpacked, so no test downloads anything.

Run: uv run pytest hosts/lib/tests/test_claude_agent_browser.py
"""

import json
import shutil
import subprocess

import yaml

from lib.estate import Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

SCRIPT = "/usr/lib/homelab/claude-agent-browser"
DEFAULTS = yaml.safe_load(
    (REPO / "hosts" / "components" / "claude-code" / "component.yml").read_text()
)["defaults"]
NODE = DEFAULTS["claude_code_agent_node_version"]
MCP = DEFAULTS["claude_code_agent_playwright_mcp_version"]
# Each stub appends its argv to calls.log, so a test reads which commands ran.
STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "{log}"\n'
# `node -p` answers the installed @playwright/mcp version; every other call only logs.
NODE_STUB = STUB + '[ "$1" = -p ] && echo "{version}"\nexit 0\n'


def render_script(tmp_path, home, homelab_ui: bool) -> str:
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-box.yml"
    overrides = {
        "claude_code_agent_user_home": str(home),
        "claude_code_agent_homelab_ui_enabled": homelab_ui,
    }
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), **overrides}))
    package = host_mode.render_host(
        "daniel-box", inventory=Inventory(host_vars=host_vars)
    )
    script = tmp_path / "claude-agent-browser"
    script.write_bytes(package.files[SCRIPT][0])
    script.chmod(0o755)
    return str(script)


def stub(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def lay_home(tmp_path, mcp_version: str = MCP):
    home = tmp_path / "home"
    log = tmp_path / "calls.log"
    bin_dir = home / ".local" / "share" / "node" / f"node-v{NODE}-linux-x64" / "bin"
    stub(bin_dir / "node", NODE_STUB.format(log=log, version=mcp_version))
    stub(bin_dir / "npm", STUB.format(log=log))
    stub(home / ".local" / "bin" / "claude", STUB.format(log=log))
    return home, log


def run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", script], capture_output=True, text=True, check=True)


def calls(log) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def registered(home) -> dict:
    """What `claude mcp add-json` would have written, as Claude Code keeps it."""
    node_bin = home / ".local" / "share" / "node" / "current" / "bin"
    return {
        "mcpServers": {
            "homelab-ui": {
                "type": "stdio",
                "command": str(
                    home / "server" / "scripts" / "diagnostics" / "ui_mcp.sh"
                ),
                "args": [],
                "env": {"NODE_BIN": str(node_bin)},
            }
        }
    }


def test_a_first_run_links_the_pinned_node_and_registers_the_server(tmp_path):
    home, log = lay_home(tmp_path, mcp_version="0.0.1")
    run(render_script(tmp_path, home, homelab_ui=True))
    node_dir = home / ".local" / "share" / "node"
    assert (node_dir / "current").readlink().name == f"node-v{NODE}-linux-x64"
    made = calls(log)
    assert f"npm install --global --no-fund --no-audit @playwright/mcp@{MCP}" in made
    assert any(
        c.startswith("claude mcp add-json --scope user homelab-ui ") for c in made
    )
    assert not any("mcp remove" in c for c in made)


def test_a_current_registration_and_package_change_nothing(tmp_path):
    home, log = lay_home(tmp_path)
    (home / ".claude.json").write_text(json.dumps(registered(home)))
    run(render_script(tmp_path, home, homelab_ui=True))
    assert not any(c.startswith(("npm ", "claude ")) for c in calls(log))


def test_a_node_bump_removes_the_old_tree(tmp_path):
    home, _ = lay_home(tmp_path)
    old = home / ".local" / "share" / "node" / "node-v1.0.0-linux-x64"
    old.mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps(registered(home)))
    run(render_script(tmp_path, home, homelab_ui=True))
    assert not old.exists()
    assert (home / ".local" / "share" / "node" / f"node-v{NODE}-linux-x64").is_dir()


def test_switching_homelab_ui_off_removes_the_registration_and_the_toolchain(tmp_path):
    home, log = lay_home(tmp_path)
    (home / ".claude.json").write_text(json.dumps(registered(home)))
    cache = home / ".cache" / "ms-playwright"
    cache.mkdir(parents=True)
    run(render_script(tmp_path, home, homelab_ui=False))
    assert calls(log) == ["claude mcp remove --scope user homelab-ui"]
    assert not (home / ".local" / "share" / "node").exists()
    assert not cache.exists()
