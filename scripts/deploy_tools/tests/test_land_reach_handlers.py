#!/usr/bin/env python3
"""A `handlers/` change reaches the hosts that run the tasks notifying it.

A `handlers/main.yml` must not fall through to the role-level reach, which for a dispatcher is
the union of both halves: a handler notified only from a half one host never runs does not
reach that host. The `_widget_role` fixture below holds that narrowing.

`_handler_notifier_chains` reads a handlers file's reach from the tasks that `notify:` the
handlers it defines, and narrows only when EVERY handler has a notifying task -- the reject
half below, because one handler resolved by name says nothing about the next.

A handler notified from both halves is NOT narrowed: `handlers/both.yml` defines `Reload units`,
and both task files notify it, so the handler really does run on every host.
`test_a_handler_notified_from_both_halves_reaches_every_host` holds that answer.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_handlers.py
"""

import pytest
import yaml

from deploy_tools.land_lib import land_reach


@pytest.fixture
def _widget_role(tmp_path):
    """A gateless role whose handlers are notified on daniel-box alone, the dispatcher shape.

        `install.yml` comes in under `when: has_gitops` and notifies both handlers of
        `handlers/main.yml`: one directly, one through the `vars:` of a cross-role
        `{{ role_path }}` import. `teardown.yml` is the other half and notifies only `Reload units`,
    which `install.yml` notifies too (`handlers/both.yml`).
        `handlers/extra.yml` is the reject half -- a handler nothing notifies, beside one
        whose only candidate notifier names a LONGER handler name.
    """
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "x", "roles": [{"role": "widget"}]}]))
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(yaml.safe_dump({"has_gitops": False}))
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    (host_vars_dir / "daniel-box.yml").write_text(yaml.safe_dump({"has_gitops": True}))
    role_dir = tmp_path / "roles" / "widget"
    (role_dir / "tasks").mkdir(parents=True)
    (role_dir / "tasks" / "main.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Install",
                    "ansible.builtin.include_tasks": "install.yml",
                    "when": "has_gitops",
                },
                {
                    "name": "Reap",
                    "ansible.builtin.include_tasks": "teardown.yml",
                    "when": "not has_gitops",
                },
            ]
        )
    )
    (role_dir / "tasks" / "install.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Install the unit",
                    "ansible.builtin.template": {
                        "src": "widget.service.j2",
                        "dest": "/etc/systemd/system/widget.service",
                    },
                    "notify": ["Reload systemd after widget teardown", "Reload units"],
                },
                {
                    "name": "Install the shared timer",
                    "ansible.builtin.import_tasks": "{{ role_path }}/../common/tasks/"
                    "check_timer.yml",
                    "vars": {"host_lib_notify": ["Run widget once"]},
                },
            ]
        )
    )
    (role_dir / "tasks" / "teardown.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Remove the unit",
                    "ansible.builtin.file": {
                        "path": "/etc/systemd/system/widget.service",
                        "state": "absent",
                    },
                    "notify": ["Reload units"],
                }
            ]
        )
    )
    (role_dir / "templates").mkdir()
    (role_dir / "templates" / "widget.service.j2").write_text("[Service]\n")
    (role_dir / "handlers").mkdir()
    (role_dir / "handlers" / "main.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Reload systemd after widget teardown",
                    "ansible.builtin.systemd": {"daemon_reload": True},
                },
                {
                    "name": "Run widget once",
                    "ansible.builtin.systemd": {
                        "name": "widget.service",
                        "state": "started",
                    },
                },
            ]
        )
    )
    (role_dir / "handlers" / "both.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Reload units",
                    "ansible.builtin.systemd": {"daemon_reload": True},
                }
            ]
        )
    )
    (role_dir / "handlers" / "extra.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Reload systemd",
                    "ansible.builtin.systemd": {"daemon_reload": True},
                },
                {
                    "name": "Restart widget",
                    "ansible.builtin.systemd": {
                        "name": "widget.service",
                        "state": "restarted",
                    },
                },
            ]
        )
    )
    return playbook, all_vars, host_vars_dir, tmp_path / "roles"


def _hosts(fixture, path):
    playbook, all_vars, host_vars_dir, roles_dir = fixture
    return land_reach.setup_file_hosts(
        "widget",
        path,
        playbook=playbook,
        all_vars=all_vars,
        host_vars_dir=host_vars_dir,
        roles_dir=roles_dir,
    )


def test_a_handlers_file_narrows_to_the_hosts_that_notify_it(_widget_role):
    """The accept half. One handler is notified by a leaf task's own `notify:`, the other
    only through the `vars:` of a cross-role import -- the shape `_gates_in` offers as a
    leaf because it does not follow the import."""
    assert _hosts(_widget_role, "ansible/roles/setup/widget/handlers/main.yml") == (
        frozenset({"daniel-box"})
    )


def test_a_handler_notified_from_both_halves_reaches_every_host(_widget_role):
    """Not every dispatcher narrows, and this one must not: the union of the two halves."""
    assert _hosts(_widget_role, "ansible/roles/setup/widget/handlers/both.yml") == (
        frozenset(land_reach._HOSTS)
    )


def test_a_handlers_file_stays_wide_when_one_handler_has_no_notifier(_widget_role):
    """The reject half, and the substring guard with it. Nothing notifies `Restart widget`
    at all; `Reload systemd` has one near-miss candidate, the task notifying `Reload systemd
    after widget teardown`, which an inexact match would count. A handler this cannot place
    may be notified through a shape it cannot read, and narrower than the truth hides an
    unconverged host."""
    assert _hosts(_widget_role, "ansible/roles/setup/widget/handlers/extra.yml") == (
        frozenset(land_reach._HOSTS)
    )


def test_a_handlers_file_that_cannot_be_read_stays_wide(_widget_role):
    """Unknown stays wide, the same asymmetry `_eval_when` applies inside one gate."""
    roles_dir = _widget_role[3]
    (roles_dir / "widget" / "handlers" / "main.yml").write_text("{{ not yaml")
    assert _hosts(_widget_role, "ansible/roles/setup/widget/handlers/main.yml") == (
        frozenset(land_reach._HOSTS)
    )


def test_a_meta_file_keeps_the_role_level_answer(_widget_role):
    """`meta/main.yml` carries no task-level evidence: no task names it and it notifies
    nothing, so there is nothing to narrow on."""
    (_widget_role[3] / "widget" / "meta").mkdir()
    (_widget_role[3] / "widget" / "meta" / "main.yml").write_text(
        yaml.safe_dump({"dependencies": []})
    )
    assert _hosts(
        _widget_role, "ansible/roles/setup/widget/meta/main.yml"
    ) == frozenset(land_reach._HOSTS)
