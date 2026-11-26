#!/usr/bin/env python3
"""Guards that the k3s nodes never get Docker installed on them.

One invariant, pinned at both layers: no k3s node's host package may ship the Docker engine,
and hosts/bootstrap-host.sh must refuse to install k3s onto a host that already has it. Each
layer is the reason the other is not enough on its own.

THE PACKAGE LAYER. k3s ships its own containerd plus flannel/kube-proxy iptables rules, and
Docker's DOCKER/DOCKER-USER chains and FORWARD-policy handling break them. The engine ships in
the `pi-docker` component and its apt source in `apt-docker`, so a k3s host's package listing
either would install a second container runtime beside k3s on its next package version.

THE INSTALL LAYER. hosts/bootstrap-host.sh refuses to install k3s on a host running Docker,
server and agent alike, and hosts/lib/tests/bootstrap/test_bootstrap_host_k3s.py holds that half. A node
without it can run a second container runtime next to k3s while every repo-side check reads
green.

Run: uv run pytest hosts/tests/k3s/test_k3s_host_has_no_docker.py
"""

import pytest
from docs.fragment_readers import hosts_listing_component

# daniel-box is the k3s server and daniel-server an agent node; both must stay Docker-free.
K3S_HOSTS = ("daniel-box", "daniel-server")
DOCKER_COMPONENTS = ("pi-docker", "apt-docker")


@pytest.mark.parametrize("component", DOCKER_COMPONENTS)
def test_no_k3s_host_lists_a_docker_component(component):
    listing = hosts_listing_component(component)
    # Non-vacuity: a renamed component would empty the listing and pass the check below.
    assert listing, f"no host package lists {component}"
    assert not set(K3S_HOSTS) & set(listing), (
        f"{component} ships the Docker engine or its apt source, and {listing} list it. A k3s "
        "node must never get Docker: k3s brings its own containerd."
    )
