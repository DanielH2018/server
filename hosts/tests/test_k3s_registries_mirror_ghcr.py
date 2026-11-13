"""Every node's registries.yaml names the registry's ghcr.io, lscr.io and docker.io mirrors.

Without the entry, containerd pulls a flux-owned GHCR image straight from ghcr.io, the mirror
holds nothing, and nut cannot cold-start during a WAN outage. The endpoint follows the same
split as the push store's: the registry node reaches the loopback hostPort, every other node
the Service's pinned ClusterIP. The ghcr.io entry is D5 Open question 2; lscr.io and
docker.io are #6257. Each node's host package ships its copy: daniel-box's since H6.4a and
daniel-server's since H6.5a.
"""

from functools import cache

import pytest
from lib import yaml_fast

from lib.repo_paths import ALL_VARS
from renderer.renderer_lib import host as host_mode

VARS = yaml_fast.safe_load(ALL_VARS.read_text())


@cache
def _mirrors(host: str) -> dict:
    """The mirrors in the registries.yaml the host's package ships (k3s-server or k3s-agent)."""
    rendered = host_mode.render_host(host).files["/etc/rancher/k3s/registries.yaml"][0]
    return yaml_fast.safe_load(rendered)["mirrors"]


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("daniel-box", "localhost"),
        ("daniel-server", VARS["k8s_registry_cluster_ip"]),
    ],
)
def test_ghcr_is_mirrored_through_the_registry(host, address):
    endpoint = f"http://{address}:{VARS.get('k8s_registry_ghcr_mirror_port')}"
    mirrors = _mirrors(host)
    ghcr = mirrors.get("ghcr.io")
    assert ghcr, f"{host}'s registries.yaml mirrors only {sorted(mirrors)}"
    assert ghcr["endpoint"] == [endpoint], ghcr


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("daniel-box", "localhost"),
        ("daniel-server", VARS["k8s_registry_cluster_ip"]),
    ],
)
def test_lscr_is_mirrored_through_the_ghcr_mirror(host, address):
    """lscr.io serves ghcr.io's repositories under ghcr.io's token realm (#6257)."""
    endpoint = f"http://{address}:{VARS['k8s_registry_ghcr_mirror_port']}"
    lscr = _mirrors(host).get("lscr.io")
    assert lscr, f"{host}'s registries.yaml has no lscr.io mirror"
    assert lscr["endpoint"] == [endpoint], lscr


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("daniel-box", "localhost"),
        ("daniel-server", VARS["k8s_registry_cluster_ip"]),
    ],
)
def test_docker_hub_is_mirrored_through_its_own_mirror(host, address):
    """docker.io pulls go to the dockerhub-mirror's port, not the ghcr.io proxy's (#6257)."""
    port = VARS.get("k8s_registry_dockerhub_mirror_port")
    assert port and port != VARS["k8s_registry_ghcr_mirror_port"], port
    docker = _mirrors(host).get("docker.io")
    assert docker, f"{host}'s registries.yaml has no docker.io mirror"
    assert docker["endpoint"] == [f"http://{address}:{port}"], docker


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("daniel-box", "localhost"),
        ("daniel-server", VARS["k8s_registry_cluster_ip"]),
    ],
)
def test_the_push_store_mirror_keeps_its_endpoint(host, address):
    """The ghcr.io entry is beside the push store's, not in place of it."""
    port = VARS["k8s_registry_port"]
    push = _mirrors(host).get(f"localhost:{port}")
    assert push, f"{host}'s registries.yaml lost the push store's mirror"
    assert push["endpoint"] == [f"http://{address}:{port}"], push
