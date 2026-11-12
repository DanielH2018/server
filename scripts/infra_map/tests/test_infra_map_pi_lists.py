#!/usr/bin/env python3
"""`declared_services` for daniel-pi reads its `compose_services` (H5).

Run: uv run pytest scripts/infra_map/tests/test_infra_map_pi_lists.py
"""

import pytest

import gen_infra_map as g

from _infra_map import GLOBALS, pi_host


ALLOY = {"name": "alloy", "port": 12345}


def test_the_pi_declares_its_compose_services_and_ignores_containers_list():
    hv = {**pi_host([ALLOY]), "containers_list": [{"name": "autoheal"}]}
    names = [s["name"] for s in g.declared_services("daniel-pi", hv, GLOBALS)]
    assert names == ["alloy"]


def test_the_pi_without_compose_services_raises():
    with pytest.raises(ValueError, match="compose_services"):
        g.declared_services("daniel-pi", {"server_ip": "10.0.0.161"}, GLOBALS)
