#!/usr/bin/env python3
"""Guards the Pi's Compose ordering against its real inventory and rendered package.

docker-proxy must be packaged beside autoheal and alloy, and each downstream's service file
must carry `depends_on` on the docker-proxy service it reaches.

Run: uv run pytest ansible/tests/deploy/test_platform_filter_real_inventory.py
"""

import functools

from lib import yaml_fast

from renderer.renderer_lib import host as host_mode
from _helpers import HOST_VARS


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
