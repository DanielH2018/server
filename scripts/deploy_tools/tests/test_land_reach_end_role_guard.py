#!/usr/bin/env python3
"""A role's leading `meta: end_role` guard takes a host out of `setup_role_hosts`.

H4 ends `nut_host` on the UPS host with a guard in the role, because an `initial_setup.yml`
edit parks every session's landing. `setup_role_hosts` reads the guard through
`setup_gates.role_end_hosts`, so a `nut_host` landing owes daniel-server no hand apply.

The accept half is a guard that passes on one host and drops it. The reject half is every
shape that must leave the host reached: a guard that is not the first task, a guard that
fails on the host, and a guard the strict evaluator cannot read.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_end_role_guard.py
"""

import pytest
import yaml

from deploy_tools.land_lib import land_reach
from deploy_tools.setup_lib import setup_gates

GUARD = {
    "name": "End the role on the UPS host",
    "ansible.builtin.meta": "end_role",
    "when": "inventory_hostname == ups_host",
}
WRITE = {
    "name": "Write the config",
    "ansible.builtin.copy": {"dest": "/etc/demo.conf", "content": "x"},
}
ALL_HOSTS = frozenset(land_reach._HOSTS)


@pytest.fixture
def tree(tmp_path):
    """A playbook running `demo` on every host, vars naming daniel-server as `ups_host`."""
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "x", "roles": ["demo"]}]))
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(yaml.safe_dump({"ups_host": "daniel-server"}))
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    (tmp_path / "roles" / "demo" / "tasks").mkdir(parents=True)

    def reach(tasks: list[dict], playbook_when: str | None = None) -> frozenset[str]:
        (tmp_path / "roles" / "demo" / "tasks" / "main.yml").write_text(
            yaml.safe_dump(tasks)
        )
        if playbook_when:
            playbook.write_text(
                yaml.safe_dump(
                    [{"hosts": "x", "roles": [{"role": "demo", "when": playbook_when}]}]
                )
            )
        return land_reach.setup_role_hosts(
            "demo", playbook, all_vars, host_vars_dir, tmp_path / "roles"
        )

    return reach


def test_the_guard_drops_the_host_it_passes_on(tree):
    assert "daniel-server" in ALL_HOSTS
    assert tree([GUARD, WRITE]) == ALL_HOSTS - {"daniel-server"}


def test_the_guard_narrows_a_playbook_gate_further(tree):
    reached = tree([GUARD, WRITE], playbook_when="inventory_hostname != 'daniel-pi'")
    assert reached == frozenset({"daniel-box"})


def test_a_guard_that_is_not_the_first_task_drops_no_host(tree):
    assert tree([WRITE, GUARD]) == ALL_HOSTS


def test_a_guard_that_fails_on_every_host_drops_no_host(tree):
    never = {**GUARD, "when": "inventory_hostname == 'no-such-host'"}
    assert tree([never, WRITE]) == ALL_HOSTS


def test_a_guard_the_strict_evaluator_cannot_read_drops_no_host(tree):
    jinja = {**GUARD, "when": "{{ ups_host }} == inventory_hostname"}
    assert tree([jinja, WRITE]) == ALL_HOSTS


def test_an_unconditional_end_role_ends_the_role_everywhere(tree):
    bare = {"ansible.builtin.meta": "end_role"}
    assert tree([bare, WRITE]) == frozenset()


def test_the_short_module_name_is_a_guard_too(tree):
    short = {"meta": "end_role", "when": GUARD["when"]}
    assert tree([short, WRITE]) == ALL_HOSTS - {"daniel-server"}


def test_a_file_the_role_ships_reaches_only_the_hosts_the_role_does(tree, tmp_path):
    tree([GUARD, WRITE])
    path = "ansible/roles/setup/demo/templates/demo.conf.j2"
    reached = land_reach.setup_file_hosts(
        "demo",
        path,
        tmp_path / "initial_setup.yml",
        tmp_path / "all.yml",
        tmp_path / "host_vars",
        tmp_path / "roles",
    )
    assert reached == ALL_HOSTS - {"daniel-server"}


def test_the_guard_reader_finds_no_guard_in_a_live_role_without_one():
    roles = land_reach._SETUP_ROLES_DIR
    assert setup_gates.role_end_guard(roles / "gitops_deploy") is None
    assert setup_gates.role_end_guard(roles / "no_such_role") is None
