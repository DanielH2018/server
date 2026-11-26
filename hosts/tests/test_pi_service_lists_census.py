#!/usr/bin/env python3
"""The Pi's services are its `compose_services`.

The `homelab-daniel-pi` package deploys the Pi's `compose_services` (H5). This census fails
when that list lacks `docker-proxy` or `alloy`. `scripts/lib/tests/k8s/test_inventory_lists.py`
holds the other half: no host_vars file defines `containers_list`, a list nothing deploys.

Run: uv run pytest hosts/tests/test_pi_service_lists_census.py
"""

from lib.estate import host_vars

HOST = "daniel-pi"
# Named members the census must find in the Pi's `compose_services`.
MUST_FIND = {"docker-proxy", "alloy"}


def test_the_pis_compose_services_hold_the_named_members():
    doc = host_vars(HOST)
    assert MUST_FIND <= {e["name"] for e in doc["compose_services"]}
