#!/usr/bin/env python3
"""Every `cluster/services.yml` entry must name a role that deploys it.

AN ENTRY WITH NO ROLE DEPLOYS NOTHING. A k8s entry needs ansible/roles/k8s/<name>/. The Pi's
services are `compose_services` entries, which the `homelab-daniel-pi` package deploys
(ADR-0013).

Run: uv run pytest ansible/tests/deploy/test_containers_list_roles_exist.py
"""

import pytest
from _helpers import ANSIBLE
from lib.render_guard import k8s_services

K8S_ROLES = ANSIBLE / "roles" / "k8s"

ENTRIES = [pytest.param(entry, id=entry["name"]) for entry in k8s_services()]


@pytest.mark.parametrize("entry", ENTRIES)
def test_entry_resolves_to_a_deployable_role(entry):
    role = K8S_ROLES / entry["name"]
    assert role.is_dir(), f"{entry['name']} is a k8s service but {role} does not exist"


def test_inventory_has_entries():
    assert ENTRIES, "no service entries found: the file or schema changed"
