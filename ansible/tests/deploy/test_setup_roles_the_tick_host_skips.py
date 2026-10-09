"""`gitops_markers.SETUP_ROLES_OFF_THE_TICK_HOST` names every role the tick's host never runs.

The deployer applies a setup role as `initial_setup.yml --tags <role>` on its own host. A role
the playbook gates off that host matches no task there and exits 0, so the tick recorded an
apply the Pi never received (#3933). The deployer cannot read the inventory, so it keeps a
static table, and this derives the table from the gates and host vars `land_reach` evaluates.
"""

from pathlib import Path

import yaml

import land_reach
from gitops_markers import SETUP_ROLES_OFF_THE_TICK_HOST
from lib.repo_paths import ALL_VARS, HOST_VARS


def off_tick_host_roles(
    playbook: Path = land_reach._INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    roles_dir: Path = land_reach._SETUP_ROLES_DIR,
) -> dict[str, frozenset[str]]:
    """{role: the hosts it reaches} for each role that reaches no `has_gitops` host."""
    tick_hosts = {
        h
        for h in land_reach._HOSTS
        if land_reach._host_vars(h, all_vars, host_vars_dir).get("has_gitops")
    }
    found = {}
    for role in land_reach._initial_setup_roles(playbook):
        reach = land_reach.setup_role_hosts(
            role, playbook, all_vars, host_vars_dir, roles_dir
        )
        if reach and not reach & tick_hosts:
            found[role] = reach
    return found


def test_the_table_is_every_role_the_tick_host_never_runs():
    derived = off_tick_host_roles()
    assert "optimize_pi" in derived, "census found no Pi-only role: wrong playbook?"
    assert derived == {
        role: frozenset({host}) for role, host in SETUP_ROLES_OFF_THE_TICK_HOST.items()
    }


def test_a_role_gated_onto_another_host_is_derived(tmp_path):
    roles_dir = tmp_path / "roles" / "setup"
    for role in ("everywhere", "pi_only"):
        (roles_dir / role / "tasks").mkdir(parents=True)
        (roles_dir / role / "tasks" / "main.yml").write_text("- name: x\n  debug: {}\n")
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "roles": [
                        {"role": "everywhere"},
                        {
                            "role": "pi_only",
                            "when": "inventory_hostname == 'daniel-pi'",
                        },
                    ],
                }
            ]
        )
    )
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("has_gitops: false\n")
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "daniel-box.yml").write_text("has_gitops: true\n")
    assert off_tick_host_roles(playbook, all_vars, host_vars, roles_dir) == {
        "pi_only": frozenset({"daniel-pi"})
    }
