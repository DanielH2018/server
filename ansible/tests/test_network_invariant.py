#!/usr/bin/env python3
"""Invariant: every Docker network a service attaches to (host_vars `containers_list[].networks`)
must be created by docker_install's "Create Docker networks" loop.

Otherwise a service on a brand-new network deploys fine on an EXISTING host (the net is
already there) but fails only on a fresh-host bring-up — this catches it pre-deploy instead.
Service-INTERNAL nets (pihole_internal, scrutiny_internal, crowdsec-db) are declared inside
their own compose and never appear in host_vars, so they're correctly out of scope here.
Mirrors validate_compose_templates' host_vars-driven model.

Run: uv run pytest ansible/tests/test_network_invariant.py
"""
import pathlib

import yaml

_ANSIBLE = pathlib.Path(__file__).resolve().parents[1]


def _created_networks() -> set[str]:
    all_vars = yaml.safe_load((_ANSIBLE / "inventory/group_vars/all.yml").read_text())
    docker_network = all_vars["docker_network"]
    tasks = yaml.safe_load((_ANSIBLE / "roles/setup/docker_install/tasks/main.yml").read_text())
    loop = next(t["loop"] for t in tasks if t.get("name") == "Create Docker networks")
    # the loop's first item is the Jinja "{{ docker_network }}"; the rest are literals
    return {docker_network if i.strip() == "{{ docker_network }}" else i.strip() for i in loop}


def _referenced_networks() -> dict[str, str]:
    refs: dict[str, str] = {}  # network -> first "host/service" that references it
    for host_file in (_ANSIBLE / "inventory/host_vars").glob("*.yml"):
        host_vars = yaml.safe_load(host_file.read_text()) or {}
        for svc in host_vars.get("containers_list") or []:
            for net in svc.get("networks") or []:
                refs.setdefault(net, f"{host_file.stem}/{svc.get('name')}")
    return refs


def test_every_referenced_network_is_created_by_docker_install():
    created = _created_networks()
    missing = {net: where for net, where in _referenced_networks().items() if net not in created}
    assert not missing, (
        "networks referenced in host_vars but NOT created by docker_install's loop "
        "(would fail only on a fresh-host deploy): %s" % missing
    )


def test_docker_install_creates_the_expected_core_networks():
    # guard against the loop being accidentally gutted
    assert {"proxy", "monitoring", "media", "apps"} <= _created_networks()
