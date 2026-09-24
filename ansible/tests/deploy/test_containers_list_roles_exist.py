#!/usr/bin/env python3
"""`containers_list` and the role trees must name the same services.

Two failures, one relation read in both directions.

AN ENTRY WITH NO ROLE DEPLOYS NOTHING, and until this test nothing said so:
validate/compose_templates.py returned "nothing to validate" for a missing template, which
prints [ok]. That is how a broken glances role shipped. The validator now errors on it; this
test is the durable half, checking the inventory against the role trees directly.

A ROLE WITH NO ENTRY reads as a service that exists and is deployed by nothing. Since the
2026-08-14 migration `roles/containers/` holds only the Pi's Docker services (repo CLAUDE.md,
*`roles/containers/` is now only the Pi*), and `containers_list` in `host_vars/daniel-pi.yml`
is the source of truth for which are deployed. That census held 6/6 at the time of writing and
was asserted nowhere. The reverse direction is asserted for the Pi alone, because it is the
only host whose role tree is a complete census of its services — a k8s role may exist for a
workload no host declares yet.

Docker entries need ansible/roles/containers/<name>/ with a compose template; k8s entries need
ansible/roles/k8s/<name>/. archive/ is not on roles_path, so a role that only survives there is
unreachable and counts as missing.

Run: uv run pytest ansible/tests/deploy/test_containers_list_roles_exist.py
"""

from pathlib import Path

import pytest
from _helpers import ANSIBLE, load_yaml


HOST_VARS = ANSIBLE / "inventory" / "host_vars"
DOCKER_ROLES = ANSIBLE / "roles" / "containers"
K8S_ROLES = ANSIBLE / "roles" / "k8s"

# `common` is the shared deploy path every Pi role includes and `archive/` holds the roles the
# migration retired; neither is a service.
NOT_SERVICES = frozenset({"common", "archive"})

# Two services the Pi has run since before the migration, so an emptied census fails by name
# rather than passing on `set() == set()`.
KNOWN_PI_SERVICES = frozenset({"wg-easy", "docker-proxy"})


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


def service_roles(roles_dir: Path) -> set[str]:
    return {
        p.name for p in roles_dir.iterdir() if p.is_dir() and p.name not in NOT_SERVICES
    }


def declared_services(host_vars: Path) -> set[str]:
    return {
        entry["name"] for entry in load_yaml(host_vars).get("containers_list") or []
    }


def test_every_pi_role_is_declared_and_every_declared_service_has_a_role():
    roles = service_roles(DOCKER_ROLES)
    declared = declared_services(HOST_VARS / "daniel-pi.yml")
    assert KNOWN_PI_SERVICES <= roles and KNOWN_PI_SERVICES <= declared
    assert roles == declared, (
        f"role without an entry: {sorted(roles - declared)}; "
        f"entry without a role: {sorted(declared - roles)}"
    )


def test_a_role_without_an_entry_is_flagged(tmp_path: Path):
    """Red-proof: the comparison fails on a stray role, and `common`/`archive` do not count."""
    for name in ("wg-easy", "stray", "common", "archive"):
        (tmp_path / "roles" / name).mkdir(parents=True)
    host_vars = tmp_path / "daniel-pi.yml"
    host_vars.write_text("containers_list:\n  - name: wg-easy\n")
    assert service_roles(tmp_path / "roles") == {"wg-easy", "stray"}
    assert declared_services(host_vars) == {"wg-easy"}
