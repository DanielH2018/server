#!/usr/bin/env python3
"""Every `containers_list` entry must name a role that deploys it.

AN ENTRY WITH NO ROLE DEPLOYS NOTHING. A k8s entry needs ansible/roles/k8s/<name>/.

A Docker entry has no deploy path at all since H5.12 deleted `ansible/roles/containers/`: the
Pi's services are `compose_services` entries, which the `homelab-daniel-pi` package deploys
(docs/target-architecture/host/h5-pi-compose.md). validate/compose_templates.py renders those,
so a Docker entry left in `containers_list` fails here.

Run: uv run pytest ansible/tests/deploy/test_containers_list_roles_exist.py
"""

import pytest
from _helpers import ANSIBLE, HOST_VARS, load_yaml


K8S_ROLES = ANSIBLE / "roles" / "k8s"


def _entries():
    for path in sorted(
        p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_")
    ):
        for entry in (load_yaml(path) or {}).get("containers_list") or []:
            if entry.get("name"):
                yield pytest.param(entry, id=f"{path.stem}:{entry['name']}")


ENTRIES = list(_entries())


@pytest.mark.parametrize("entry", ENTRIES)
def test_entry_resolves_to_a_deployable_role(entry):
    name = entry["name"]
    assert entry.get("platform") == "k8s", (
        f"{name} is a Docker containers_list entry, which nothing deploys since H5.12 retired "
        "the Ansible Compose path; a Pi service belongs in compose_services"
    )
    role = K8S_ROLES / name
    assert role.is_dir(), f"{name} is platform: k8s but {role} does not exist"


def test_inventory_has_entries():
    assert ENTRIES, "no containers_list entries found — the glob or schema changed"
