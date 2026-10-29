"""The `k3s-gates` component (#6413): every node a k3s Plan selects ships the gate script.

The k3s-agent Plan's `prepare` runs /host/usr/lib/homelab/k3s_gates.py on daniel-server. Until
#6413 only `k3s-restart` shipped the file, and daniel-server's package does not list that
component, so H6.3d's first merge failed `prepare`. The renderer already refuses one path from
two components (hosts/lib/tests/test_kuma_push_component.py).

Run: uv run pytest hosts/lib/tests/test_k3s_gates_component.py
"""

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


def test_the_restart_component_no_longer_ships_the_gates():
    component = yaml.safe_load(
        (REPO / "hosts/components/k3s-restart/component.yml").read_text()
    )
    assert GATES not in [entry["dst"] for entry in component["files"]]


@pytest.fixture(scope="module")
def server() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


def test_daniel_server_ships_the_gates_without_the_restart_unit(server):
    body, mode = server.files[GATES]
    assert body == (REPO / "hosts/lib/k3s_gates.py").read_bytes()
    assert mode == 0o755
    # Without the unit, /run/homelab-k3s-restart/ never exists, so the agent gate's marker check
    # passes by construction.
    assert "/usr/lib/systemd/system/homelab-k3s-restart.service" not in server.files
