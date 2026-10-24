#!/usr/bin/env python3
"""`declared_services` for daniel-pi reads both of its service lists (H5.1).

Run: uv run pytest scripts/infra_map/tests/test_infra_map_pi_lists.py
"""

import pytest

import gen_infra_map as g
from lib.ansible_inventory import containers_entries_in

from _infra_map import GLOBALS, pi_host


ALLOY = {"name": "alloy", "port": 12345}


def test_the_pi_declares_a_service_that_moved_to_compose_services():
    """ACCEPT: alloy in the Pi's second key keeps its declaration (H5.1)."""
    hv = pi_host([{"name": "autoheal"}], [ALLOY])
    names = [s["name"] for s in g.declared_services("daniel-pi", hv, GLOBALS)]
    assert names == ["autoheal", "alloy"]


def test_a_declared_services_reading_containers_list_alone_loses_the_moved_service():
    """REJECT: the case above fails a reader that does not read both keys."""
    hv = pi_host([{"name": "autoheal"}], [ALLOY])
    alone = [e["name"] for e in containers_entries_in(hv)]
    assert "alloy" not in alone
    assert "alloy" in [s["name"] for s in g.declared_services("daniel-pi", hv, GLOBALS)]


@pytest.mark.parametrize("missing", ["containers_list", "compose_services"])
def test_the_pi_without_either_service_key_raises(missing):
    hv = {k: v for k, v in pi_host([], []).items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        g.declared_services("daniel-pi", hv, GLOBALS)
