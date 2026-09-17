"""`roles/containers/<svc>/` and daniel-pi's `containers_list` name the same services.

Since the 2026-08-14 migration `roles/containers/` holds only the Pi's Docker services (repo
CLAUDE.md, *`roles/containers/` is now only the Pi*), and `containers_list` in
`host_vars/daniel-pi.yml` is the source of truth for which are deployed. The two drift in
opposite directions and neither is reported: a role with no entry deploys nothing and reads as
a service that exists, and an entry with no role fails only at deploy time on the Pi. Held 6/6
at the time of writing, and asserted nowhere.

`common` is the shared deploy path every Pi role includes and `archive/` holds the roles the
migration retired; neither is a service.

Run: uv run pytest ansible/tests/setup/test_pi_container_roles_match_its_containers_list.py
"""

from pathlib import Path

from _helpers import CONTAINER_ROLES, HOST_VARS, load_yaml

NOT_SERVICES = frozenset({"common", "archive"})

# Two services the Pi has run since before the migration, so an emptied census fails by name
# rather than passing on `set() == set()`.
KNOWN_PI_SERVICES = frozenset({"wg-easy", "docker-proxy"})


def service_roles(roles_dir: Path) -> set[str]:
    return {
        p.name for p in roles_dir.iterdir() if p.is_dir() and p.name not in NOT_SERVICES
    }


def declared_services(host_vars: Path) -> set[str]:
    return {
        entry["name"] for entry in load_yaml(host_vars).get("containers_list") or []
    }


def test_every_pi_role_is_declared_and_every_declared_service_has_a_role():
    roles = service_roles(CONTAINER_ROLES)
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
