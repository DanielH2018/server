#!/usr/bin/env python3
"""Every inventory entry must resolve to a role that can actually deploy it.

An entry naming a role that was moved, renamed or retired deploys nothing, and
until now nothing said so: validate_compose_templates.py returned "nothing to
validate" for a missing template, which prints [ok]. That is how a broken
glances role shipped. The validator now errors on it; this test is the durable
half, checking the inventory against the role trees directly.

Docker entries need ansible/roles/containers/<name>/ with a compose template;
k8s entries need ansible/roles/k8s/<name>/. archive/ is not on roles_path, so a
role that only survives there is unreachable and counts as missing.

Run: uv run pytest ansible/tests/deploy/test_containers_list_roles_exist.py
"""

import pytest
import yaml
from _helpers import ANSIBLE


HOST_VARS = ANSIBLE / "inventory" / "host_vars"
DOCKER_ROLES = ANSIBLE / "roles" / "containers"
K8S_ROLES = ANSIBLE / "roles" / "k8s"


def _entries():
    for path in sorted(
        p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_")
    ):
        for entry in (yaml.safe_load(path.read_text()) or {}).get(
            "containers_list"
        ) or []:
            if entry.get("name"):
                yield pytest.param(entry, id=f"{path.stem}:{entry['name']}")


ENTRIES = list(_entries())


@pytest.mark.parametrize("entry", ENTRIES)
def test_entry_resolves_to_a_deployable_role(entry):
    name = entry["name"]
    if entry.get("platform") == "k8s":
        role = K8S_ROLES / name
        assert role.is_dir(), f"{name} is platform: k8s but {role} does not exist"
        return

    role = DOCKER_ROLES / name
    assert role.is_dir(), f"{name} is a Docker entry but {role} does not exist"
    compose = role / "templates" / "docker-compose.yml.j2"
    assert compose.is_file(), (
        f"{name} has no {compose}, so a deploy renders no container"
    )


def test_inventory_has_entries():
    assert ENTRIES, "no containers_list entries found — the glob or schema changed"
