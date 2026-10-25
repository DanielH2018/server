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

import re

import pytest
from lib import yaml_fast

from toposort import filter_by_platform
from _helpers import HOST_VARS, REPO


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


# The Docker play deploys containers_list in order, and the package's Compose project (H5) runs
# `compose_services` through homelab-compose.service, independent of the play. Each pair is
# (upstream, downstream): autoheal restarts containers through docker-proxy-lifecycle, and alloy
# discovers logs through docker-proxy. From H5.6 an upstream moves no later than its downstream:
#   - both in containers_list: the upstream is listed first (the play's order);
#   - the upstream in compose_services: the downstream is in either key, and a downstream that is
#     also in compose_services carries `depends_on` on the service it reaches;
#   - the upstream in containers_list and the downstream in compose_services is a violation.
PI_ORDERING = [("docker-proxy", "autoheal"), ("docker-proxy", "alloy")]
# What a packaged downstream's `depends_on` must name.
PI_DEPENDS_ON = {"autoheal": "docker-proxy-lifecycle", "alloy": "docker-proxy"}
SERVICES = REPO / "hosts" / "components" / "pi-compose" / "services"


def _order_violations(listed, packaged, edges, depends_on=None):
    """Edges whose upstream is absent, later than its downstream, or in the wrong key.

    Args:
        listed: Names in the Pi's `containers_list`, in play order.
        packaged: Names in its `compose_services`.
        edges: (upstream, downstream) pairs.
        depends_on: Maps a downstream in `packaged` to the `depends_on` names its service file
            declares; None skips that check.
    """
    out = []
    for up, down in edges:
        if up in packaged:
            if down not in listed and down not in packaged:
                out.append((up, down))
            elif down in packaged and depends_on is not None:
                if PI_DEPENDS_ON[down] not in depends_on.get(down, ()):
                    out.append((up, down))
        elif up in listed and down in listed:
            if listed.index(up) > listed.index(down):
                out.append((up, down))
        else:
            out.append((up, down))
    return out


def _pi_lists():
    pi = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())
    listed = [c["name"] for c in filter_by_platform(pi.get("containers_list") or [])]
    return listed, [c["name"] for c in pi.get("compose_services") or []]


def _service_depends_on(name):
    # Template lines (`{% ... %}` and `{# ... #}` blocks) are dropped; the rest is plain YAML
    # for these services.
    text = (SERVICES / name / "compose.yaml.j2").read_text()
    body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("{%"))
    return yaml_fast.safe_load(body)["services"][name].get("depends_on") or []


def test_daniel_pi_moves_every_upstream_no_later_than_its_downstream():
    listed, packaged = _pi_lists()
    depends = {n: _service_depends_on(n) for n in packaged if n in PI_DEPENDS_ON}

    assert _order_violations(listed, packaged, PI_ORDERING, depends) == []


def test_the_census_finds_docker_proxy_packaged_and_its_downstreams_listed():
    listed, packaged = _pi_lists()

    assert "docker-proxy" in packaged
    assert {"autoheal", "alloy"} <= set(listed)


def test_an_upstream_listed_after_its_downstream_is_a_violation():
    names = ["autoheal", "docker-proxy", "alloy"]

    assert _order_violations(names, [], PI_ORDERING) == [("docker-proxy", "autoheal")]


def test_an_upstream_left_in_the_list_while_its_downstream_moves_is_a_violation():
    assert _order_violations(["docker-proxy"], ["autoheal", "alloy"], PI_ORDERING) == [
        ("docker-proxy", "autoheal"),
        ("docker-proxy", "alloy"),
    ]


def test_a_packaged_downstream_without_depends_on_its_upstream_is_a_violation():
    packaged = ["docker-proxy", "autoheal"]
    edge = PI_ORDERING[:1]

    assert _order_violations([], packaged, edge, {"autoheal": []}) == edge
    wired = {"autoheal": ["docker-proxy-lifecycle"]}
    assert _order_violations([], packaged, edge, wired) == []
