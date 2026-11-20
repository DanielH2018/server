#!/usr/bin/env python3
"""The Pi's services are its `compose_services`.

The `homelab-daniel-pi` package deploys the Pi's `compose_services` (H5). This census fails
when that list lacks `docker-proxy` or `alloy`. `scripts/lib/tests/test_inventory_lists.py`
holds the other half: no host_vars file defines `containers_list`, a list nothing deploys.

Run: uv run pytest hosts/tests/test_pi_service_lists_census.py
"""

from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import HOST_VARS

HOST = "daniel-pi"
# Named members the census must find in the Pi's `compose_services`.
MUST_FIND = {"docker-proxy", "alloy"}


def host_doc(path: Path) -> dict:
    return yaml_fast.safe_load(path.read_text()) or {}


def test_the_pis_compose_services_hold_the_named_members():
    doc = host_doc(HOST_VARS / f"{HOST}.yml")
    assert MUST_FIND <= {e["name"] for e in doc["compose_services"]}
