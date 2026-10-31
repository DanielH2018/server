"""The claude-code host package, rendered with host_vars overrides, for the agent-user tests."""

import shutil
from pathlib import Path

import yaml
from lib import yaml_fast
from lib.estate import Inventory
from lib.repo_paths import HOST_VARS
from renderer.renderer_lib import host as host_mode

SETUP = "/usr/lib/homelab/claude-agent-setup"


def render_daniel_server_with(tmp_path: Path, **overrides: object):
    """The daniel-server package rendered with `overrides` laid over its host_vars."""
    host_vars = tmp_path / "host_vars"
    shutil.copytree(HOST_VARS, host_vars)
    path = host_vars / "daniel-server.yml"
    path.write_text(
        yaml.safe_dump({**yaml_fast.safe_load(path.read_text()), **overrides})
    )
    return host_mode.render_host(
        "daniel-server", inventory=Inventory(host_vars=host_vars)
    )


def agent_setup_script(tmp_path: Path, **overrides: object) -> str:
    """The text of `claude-agent-setup` in the daniel-server package with `overrides`."""
    package = render_daniel_server_with(tmp_path, **overrides)
    return package.files[SETUP][0].decode()
