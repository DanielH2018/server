#!/usr/bin/env python3
"""`packaged_guard_hosts` reads a `<role>_packaged_hosts` guard with the role's defaults loaded.

H7 moves one subject at a time onto a host package and guards the Ansible task with
`when: inventory_hostname not in <role>_packaged_hosts['<subject>']`. The map lives in the role's
`defaults/main.yml`, below `group_vars`, and `eval_when_strict` reads only `group_vars/all.yml`
and `host_vars` unless it is given the defaults file. The accept half is a guard that excludes
the listed host. The reject half is every shape that must leave a host included or raise.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_gates_packaged_guard.py
"""

import pytest

from deploy_tools.setup_lib import setup_gates

GUARD = "inventory_hostname not in demo_packaged_hosts['ssh']"
HOSTS = ["daniel-pi", "daniel-server", "daniel-box"]


@pytest.fixture
def tree(tmp_path):
    role = tmp_path / "demo"
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text(
        "demo_packaged_hosts:\n  ssh: [daniel-pi]\n"
    )
    (tmp_path / "all.yml").write_text("other: 1\n")
    (tmp_path / "host_vars").mkdir()
    return role, tmp_path / "all.yml", tmp_path / "host_vars"


def excluded(whens, tree, hosts=HOSTS):
    role, all_vars, host_vars = tree
    return setup_gates.packaged_guard_hosts(whens, hosts, role, all_vars, host_vars)


def test_a_guard_excludes_the_hosts_the_map_lists_and_no_other(tree):
    assert excluded([GUARD], tree) == {"daniel-pi"}


def test_a_list_valued_when_is_read_item_by_item(tree):
    assert excluded([[GUARD, "ansible_check_mode"]], tree) == {"daniel-pi"}


def test_a_when_that_names_no_map_is_left_alone_even_if_it_cannot_be_read(tree):
    assert excluded(["some_registered.stat.exists", "has_x | bool"], tree) == set()


def test_a_host_vars_entry_overrides_the_role_default(tree):
    host_vars = tree[2]
    (host_vars / "daniel-server.yml").write_text(
        "demo_packaged_hosts:\n  ssh: [daniel-server]\n"
    )

    assert excluded([GUARD], tree) == {"daniel-pi", "daniel-server"}


def test_a_guard_whose_map_is_defined_nowhere_raises_instead_of_reading_true(tmp_path):
    role = tmp_path / "bare"
    role.mkdir()
    (tmp_path / "all.yml").write_text("other: 1\n")
    (tmp_path / "host_vars").mkdir()

    with pytest.raises(setup_gates.GateUnreadable):
        setup_gates.packaged_guard_hosts(
            [GUARD], HOSTS, role, tmp_path / "all.yml", tmp_path / "host_vars"
        )


def test_without_the_role_defaults_the_evaluator_cannot_see_the_map(tree):
    """The red half: `eval_when_strict` alone raises, which is why the defaults file exists."""
    _, all_vars, host_vars = tree

    with pytest.raises(setup_gates.GateUnreadable):
        setup_gates.eval_when_strict(GUARD, "daniel-pi", all_vars, host_vars)
