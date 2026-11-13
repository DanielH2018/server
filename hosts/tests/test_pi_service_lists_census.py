#!/usr/bin/env python3
"""The Pi's services are its `compose_services`, and no host_vars file lists `containers_list`.

The `homelab-daniel-pi` package deploys the Pi's `compose_services` (H5). H7.20d deleted the
Pi's empty `containers_list`, and H1.9 had already moved the cluster's list to
`cluster/services.yml`, so a `containers_list` key in any host_vars file is a list nothing
deploys. This census fails on three conditions:

- a host_vars file defines `containers_list`;
- the Pi's `compose_services` lacks `docker-proxy` or `alloy`;
- a `compose_services` entry has no legacy entry for its Ansible-era compose file in the Pi
  package.

Run: uv run pytest hosts/tests/test_pi_service_lists_census.py
"""

from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import HOST_VARS
from renderer.renderer_lib import host as host_mode

HOST = "daniel-pi"
# Named members the census must find in the Pi's `compose_services`.
MUST_FIND = {"docker-proxy", "alloy"}


def host_doc(path: Path) -> dict:
    return yaml_fast.safe_load(path.read_text()) or {}


def defining_containers_list(host_vars: Path) -> list[str]:
    """The host_vars files under `host_vars` that define `containers_list`, by stem."""
    return sorted(
        path.stem
        for path in host_vars.glob("*.yml")
        if "containers_list" in host_doc(path)
    )


def without_legacy(doc: dict, legacy: list[dict]) -> set[str]:
    """The `compose_services` names whose Ansible-era compose file no legacy entry retires."""
    paths = [entry.get("path", "") for entry in legacy]
    return {
        entry["name"]
        for entry in doc["compose_services"] or []
        if not any(
            p.endswith(f"/containers/{entry['name']}/docker-compose.yml") for p in paths
        )
    }


def test_no_host_vars_file_defines_containers_list():
    assert defining_containers_list(HOST_VARS) == []


def test_a_host_vars_file_with_a_pi_containers_list_is_flagged(tmp_path):
    (tmp_path / f"{HOST}.yml").write_text("containers_list: []\ncompose_services: []\n")
    (tmp_path / "daniel-box.yml").write_text("server_ip: 10.0.0.2\n")
    assert defining_containers_list(tmp_path) == [HOST]


def test_the_pis_compose_services_hold_the_named_members():
    doc = host_doc(HOST_VARS / f"{HOST}.yml")
    assert MUST_FIND <= {e["name"] for e in doc["compose_services"]}


def test_every_compose_service_has_a_legacy_entry():
    legacy = host_mode.render_host(HOST).legacy
    assert without_legacy(host_doc(HOST_VARS / f"{HOST}.yml"), legacy) == set()


def test_a_compose_service_with_no_legacy_entry_is_flagged():
    legacy = [{"path": "/home/ubuntu/server/containers/alloy/docker-compose.yml"}]
    doc = {"compose_services": [{"name": "alloy"}, {"name": "autoheal"}]}
    assert without_legacy(doc, legacy) == {"autoheal"}
