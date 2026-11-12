#!/usr/bin/env python3
"""Guards filter_by_platform against the real inventory, not a synthetic list.

deploy.yml runs this filter over every host's containers_list, and daniel-server
has has_gitops: true — a 30-minute timer pulls master and deploys. So a filter
that silently dropped entries would take services down with no manual gate in
between. These tests assert the filter is a no-op for every entry that hasn't
been explicitly migrated, using the inventory files as they actually are.

They stay correct as services migrate: an entry only leaves the docker set by
gaining `platform: k8s`, which is exactly what the assertions check for.

Run: uv run pytest ansible/tests/deploy/test_platform_filter_real_inventory.py
"""

import functools

import pytest
from lib import yaml_fast

from renderer.renderer_lib import host as host_mode
from toposort import filter_by_platform
from _helpers import HOST_VARS


def _host_var_files():
    return sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))


def _containers(path):
    return (yaml_fast.safe_load(path.read_text()) or {}).get("containers_list") or []


@pytest.mark.parametrize("path", _host_var_files(), ids=lambda p: p.stem)
def test_docker_filter_keeps_every_unmigrated_entry_in_order(path):
    containers = _containers(path)
    expected = [
        c["name"] for c in containers if c.get("platform", "docker") == "docker"
    ]

    got = [c["name"] for c in filter_by_platform(containers, "docker")]

    assert got == expected


@pytest.mark.parametrize("path", _host_var_files(), ids=lambda p: p.stem)
def test_every_entry_lands_in_exactly_one_platform(path):
    containers = _containers(path)

    docker = filter_by_platform(containers, "docker")
    k8s = filter_by_platform(containers, "k8s")

    assert len(docker) + len(k8s) == len(containers), (
        "an entry carries a platform value that is neither docker nor k8s, so "
        "deploy.yml would skip it and no k8s manifest would claim it"
    )


def test_daniel_server_is_fully_drained():
    # daniel-server runs no Docker and is a k3s AGENT: cluster workloads are declared on
    # daniel-box, the control-plane node that runs the k8s play, so a k8s entry here would be
    # deployed by neither play.
    containers = _containers(HOST_VARS / "daniel-server.yml")

    # The count stays hardcoded so any entry that ever reappears here is a deliberate act, not
    # drift.
    assert len(containers) == 0
    assert len(filter_by_platform(containers, "docker")) == 0
    assert filter_by_platform(containers, "k8s") == []


# The Pi's package runs `compose_services` as one Compose project (H5) through
# homelab-compose.service. Each pair is (upstream, downstream): autoheal restarts containers
# through docker-proxy-lifecycle, and alloy discovers logs through docker-proxy. Both ends must
# be packaged, and the downstream carries `depends_on` on the service it reaches.
PI_ORDERING = [("docker-proxy", "autoheal"), ("docker-proxy", "alloy")]
# What a packaged downstream's `depends_on` must name.
PI_DEPENDS_ON = {"autoheal": "docker-proxy-lifecycle", "alloy": "docker-proxy"}
COMPOSE = "/usr/lib/homelab/compose"


def _order_violations(packaged, edges, depends_on=None):
    """Edges with an end missing from `packaged`, or a downstream without its `depends_on`.

    Args:
        packaged: Names in the Pi's `compose_services`.
        edges: (upstream, downstream) pairs.
        depends_on: Maps a downstream in `packaged` to the `depends_on` names its service file
            declares; None skips that check.
    """
    out = []
    for up, down in edges:
        if up not in packaged or down not in packaged:
            out.append((up, down))
        elif depends_on is not None and PI_DEPENDS_ON[down] not in depends_on.get(
            down, ()
        ):
            out.append((up, down))
    return out


def _pi_services():
    pi = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())
    return [c["name"] for c in pi["compose_services"] or []]


@functools.cache
def _packaged_services():
    """Every service of the Pi's rendered Compose project, by name."""
    package = host_mode.render_host("daniel-pi")
    top = yaml_fast.safe_load(package.files[f"{COMPOSE}/compose.yaml"][0].decode())
    services = {}
    for include in top["include"]:
        body = yaml_fast.safe_load(package.files[include["path"]][0].decode())
        services |= body["services"]
    return services


def _service_depends_on(name):
    # A list or a mapping keyed by service name; iterating either yields the names.
    return list(_packaged_services()[name].get("depends_on") or [])


def test_daniel_pi_packages_every_upstream_beside_its_downstream():
    packaged = _pi_services()
    depends = {n: _service_depends_on(n) for n in packaged if n in PI_DEPENDS_ON}

    assert _order_violations(packaged, PI_ORDERING, depends) == []


def test_the_census_finds_docker_proxy_autoheal_and_alloy_packaged_with_their_depends_on():
    """Named members: autoheal (H5.7) and alloy (H5.8) exercise the packaged-downstream branch.

    `depends_on` comes from the rendered Pi project, never from template source.
    """
    packaged = _pi_services()

    assert {"docker-proxy", "autoheal", "alloy"} <= set(packaged)
    depends = {n: _service_depends_on(n) for n in packaged if n in PI_DEPENDS_ON}
    assert "docker-proxy-lifecycle" in depends["autoheal"]
    assert "docker-proxy" in depends["alloy"]


def test_an_upstream_missing_from_the_package_is_a_violation():
    assert _order_violations(["autoheal", "alloy"], PI_ORDERING) == PI_ORDERING


def test_a_packaged_downstream_without_depends_on_its_upstream_is_a_violation():
    packaged = ["docker-proxy", "autoheal"]
    edge = PI_ORDERING[:1]

    assert _order_violations(packaged, edge, {"autoheal": []}) == edge
    wired = {"autoheal": ["docker-proxy-lifecycle"]}
    assert _order_violations(packaged, edge, wired) == []
