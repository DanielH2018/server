"""`ui_mcp.sh` installs the browser its playwright-mcp drives before the server starts.

Each test runs the wrapper with `NODE_BIN` pointing at stub `playwright-mcp` and `uv` binaries,
and `UI_MCP_STATE_PATH` set so no login is minted. Nothing reaches the network.
"""

import os
import subprocess

from lib.proc_testing import write_exec
from lib.repo_paths import REPO

WRAPPER = REPO / "scripts" / "diagnostics" / "ui_mcp.sh"


def launch(
    tmp_path, install_exit: int
) -> tuple[subprocess.CompletedProcess, list[str]]:
    node_bin = tmp_path / "bin"
    log = tmp_path / "calls.log"
    write_exec(
        node_bin / "playwright-mcp",
        f'#!/bin/sh\necho "$*" >> "{log}"\n'
        f'[ "$1" = install-browser ] && echo "downloading chromium" && exit {install_exit}\n'
        "exit 0\n",
    )
    write_exec(node_bin / "uv", "#!/bin/sh\necho example.test\n")
    env = {
        **os.environ,
        "NODE_BIN": str(node_bin),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "UI_MCP_STATE_PATH": str(tmp_path / "state.json"),
    }
    result = subprocess.run(
        ["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=60
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_the_browser_is_installed_before_the_server_starts(tmp_path):
    result, calls = launch(tmp_path, install_exit=0)
    assert result.returncode == 0, result.stderr
    assert calls[0] == "install-browser chromium"
    assert calls[1].startswith("--config ")
    assert "downloading chromium" not in result.stdout
    assert "downloading chromium" in result.stderr


def test_a_failed_install_starts_no_server(tmp_path):
    result, calls = launch(tmp_path, install_exit=1)
    assert result.returncode != 0
    assert calls == ["install-browser chromium"]
