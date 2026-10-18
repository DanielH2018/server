"""Every node's registries.yaml names the registry pod's ghcr.io mirror (D5 Open question 2).

Without the entry, containerd pulls a flux-owned GHCR image straight from ghcr.io, the mirror
holds nothing, and nut cannot cold-start during a WAN outage. The endpoint follows the same
split as the push store's: the registry node reaches the loopback hostPort, every other node
the Service's pinned ClusterIP.
"""

import pytest
from lib import yaml_fast

from _setup_render import render_setup_text
from lib.repo_paths import ALL_VARS

VARS = yaml_fast.safe_load(ALL_VARS.read_text())
PORT = VARS["k8s_registry_ghcr_mirror_port"]


def _mirrors(host: str) -> dict:
    text = render_setup_text("k3s", "registries.yaml.j2", {"inventory_hostname": host})
    return yaml_fast.safe_load(text)["mirrors"]


@pytest.mark.parametrize(
    ("host", "endpoint"),
    [
        ("daniel-box", f"http://localhost:{PORT}"),
        ("daniel-server", f"http://{VARS['k8s_registry_cluster_ip']}:{PORT}"),
    ],
)
def test_ghcr_is_mirrored_through_the_registry(host, endpoint):
    mirrors = _mirrors(host)
    ghcr = mirrors.get("ghcr.io")
    assert ghcr, f"{host}'s registries.yaml mirrors only {sorted(mirrors)}"
    assert ghcr["endpoint"] == [endpoint], ghcr


def test_the_push_store_mirror_is_unchanged():
    """The ghcr.io entry is beside the push store's, not in place of it."""
    assert _mirrors("daniel-server").get("localhost:5000")
