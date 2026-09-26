#!/usr/bin/env python3
"""A `handlers/` change reaches the hosts that run the tasks notifying it (#2624).

WHAT WENT WRONG. `setup_file_hosts` narrowed `templates/`, `files/`, `tasks/` and (since
#2610) `defaults/`/`vars/` to the hosts that actually run the task behind each path. A
`handlers/main.yml` fell through to the role-level reach, which for a dispatcher is the
union of both halves. `docker_install/handlers/main.yml` read as all three hosts while its
only handler is notified from `teardown.yml`, the `not has_docker` half, which daniel-pi
never runs.

`_handler_notifier_chains` reads a handlers file's reach from the tasks that `notify:` the
handlers it defines, and narrows only when EVERY handler has a notifying task -- the reject
half below, because one handler resolved by name says nothing about the next.

WHAT THIS DOES NOT FIX. Issue #2624's Verify-by asks for
`setup_file_hosts('gitops_deploy', '.../handlers/main.yml') == {'daniel-box'}`. That answer
is wrong, and `test_the_deployer_handlers_still_reach_every_host` holds the measured one:
`gitops_deploy/handlers/main.yml` defines `Reload systemd` as well as `Run gitops-deploy
once`, and `tasks/teardown.yml` notifies `Reload systemd` under `when: not has_gitops`
(693a0bcc2, 2026-09-09). The handler really does run on daniel-server and daniel-pi.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_handlers.py
"""

from pathlib import Path

import pytest
import yaml

import land_reach

REPO_ROOT = Path(__file__).resolve().parents[3]

# Every setup role that defines handlers, so a rename or a new role cannot leave the live
# cases below checking nothing.
_ROLES_WITH_HANDLERS = frozenset(
    {
        "claude_code",
        "deploy_ui",
        "docker_install",
        "gitops_deploy",
        "k3s",
        "renovate_agent",
        "renovate_notify",
    }
)


def test_every_role_with_handlers_is_still_named_here():
    """Non-vacuity: the live cases below name two of these roles by path."""
    found = {
        path.parents[1].name
        for path in (REPO_ROOT / "ansible" / "roles" / "setup").glob(
            "*/handlers/main.yml"
        )
    }
    assert found >= _ROLES_WITH_HANDLERS, f"handlers moved or went away: {found}"


def test_the_docker_teardown_handler_skips_the_only_docker_host():
    """The accept half against the live tree. `Reload systemd after docker teardown` is
    notified from `tasks/teardown.yml` alone, which `tasks/main.yml` imports under `when:
    not has_docker` -- true on daniel-box and daniel-server, false on daniel-pi."""
    assert land_reach.setup_file_hosts(
        "docker_install", "ansible/roles/setup/docker_install/handlers/main.yml"
    ) == frozenset({"daniel-box", "daniel-server"})


def test_the_deployer_handlers_still_reach_every_host():
    """Not every dispatcher narrows, and this one must not. `Reload systemd` is notified
    from both halves of `gitops_deploy`, so the file's reach is the union -- the answer
    #2624's Verify-by asks to replace with `{daniel-box}`."""
    assert land_reach.setup_file_hosts(
        "gitops_deploy", "ansible/roles/setup/gitops_deploy/handlers/main.yml"
    ) == frozenset(land_reach._HOSTS)


@pytest.fixture
def _widget_role(tmp_path):
    """A gateless role whose handlers are notified on daniel-box alone, the dispatcher shape.

    `install.yml` comes in under `when: has_gitops` and notifies both handlers of
    `handlers/main.yml`: one directly, one through the `vars:` of a cross-role
    `{{ role_path }}` import. `teardown.yml` is the other half and notifies neither.
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
                    "notify": ["Reload systemd after widget teardown"],
                },
                {
                    "name": "Install the shared timer",
                    "ansible.builtin.import_tasks": "{{ role_path }}/../common/tasks/"
                    "kuma_check_timer.yml",
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
