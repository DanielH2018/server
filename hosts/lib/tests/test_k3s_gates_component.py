"""The `k3s-gates` component: every node a k3s Plan selects ships the gate script.

The k3s-agent Plan's `prepare` runs /host/usr/lib/homelab/k3s_gates.py on daniel-server, whose
package does not list `k3s-restart`, so a component of its own must ship the file. The renderer
already refuses one path from two components (scripts/renderer/tests/host/test_renderer_host_mode.py).

Run: uv run pytest hosts/lib/tests/test_k3s_gates_component.py
"""

import os
import subprocess
import sys

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

GATES = "/usr/lib/homelab/k3s_gates.py"


def _packages_listing(component: str) -> dict[str, list[str]]:
    found = {}
    for path in sorted((REPO / "hosts").glob("*/package.yml")):
        components = yaml.safe_load(path.read_text()).get("components", [])
        if component in components:
            found[path.parent.name] = components
    return found


def test_every_package_listing_k3s_restart_also_lists_k3s_gates():
    restart = _packages_listing("k3s-restart")
    # A named member, so an empty census cannot pass.
    assert "daniel-box" in restart
    missing = [
        host for host, components in restart.items() if "k3s-gates" not in components
    ]
    assert not missing, f"list k3s-gates beside k3s-restart in: {missing}"


@pytest.fixture(scope="module")
def server() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


def test_daniel_server_ships_the_gates_without_the_restart_unit(server):
    body, mode = server.files[GATES]
    assert body == (REPO / "hosts/lib/k3s_gates.py").read_bytes()
    assert mode == 0o755
    # Without the unit /run/homelab-k3s-restart/ never exists, so the agent gate's marker check
    # passes by construction.
    assert "/usr/lib/systemd/system/homelab-k3s-restart.service" not in server.files


def test_the_shipped_gates_import_from_the_package_layout_alone(server, tmp_path):
    for dst, (body, _mode) in server.files.items():
        if dst.startswith("/usr/lib/homelab/") and dst.endswith(".py"):
            target = tmp_path / dst.removeprefix("/usr/lib/homelab/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    # `-S` drops the venv's `.pth`, so only the files the package installs beside the script resolve
    # its imports.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-S", str(tmp_path / "k3s_gates.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
