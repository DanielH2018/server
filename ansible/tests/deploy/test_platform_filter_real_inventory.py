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

import pytest
from lib import yaml_fast

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


# The Docker play deploys in containers_list order, with no dependency resolution, so the Pi's
# list order IS its deploy order. Each pair is (upstream, downstream): autoheal restarts
# containers through docker-proxy-lifecycle, and alloy discovers logs through docker-proxy.
PI_ORDERING = [("docker-proxy", "autoheal"), ("docker-proxy", "alloy")]


def _order_violations(names, edges):
    return [
        (up, down)
        for up, down in edges
        if up not in names or down not in names or names.index(up) > names.index(down)
    ]


def test_daniel_pi_lists_every_upstream_before_its_downstream():
    names = [
        c["name"] for c in filter_by_platform(_containers(HOST_VARS / "daniel-pi.yml"))
    ]

    assert _order_violations(names, PI_ORDERING) == []


def test_an_upstream_listed_after_its_downstream_is_a_violation():
    names = ["autoheal", "docker-proxy", "alloy"]

    assert _order_violations(names, PI_ORDERING) == [("docker-proxy", "autoheal")]
