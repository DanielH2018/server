#!/usr/bin/env python3
"""A `defaults/`/`vars/` change reaches the hosts that run the tasks consuming its vars (#2610).

WHAT WENT WRONG. Landing PR #2553 ended `needs-manual-apply`, prescribing
`initial_setup.yml --tags gitops_deploy` on daniel-server and daniel-pi. Both hosts set
`has_gitops: false`, where the role runs `teardown.yml` alone, so each command re-runs an
idempotent teardown and applies none of the PR. The path that widened the note is
`gitops_deploy/defaults/main.yml`: no task names a defaults file, so `setup_file_hosts` fell
through to the role-level reach, which for this dispatcher is the union of both halves --
every host.

Issue #2610 read the cause as `_gates_in` not following an `include_tasks` gate. It was not:
`_IMPORT_KEYS` has carried `include_tasks` since 693a0bcc2 (2026-09-09), and every
`gitops_deploy/files/*.py` path reads as daniel-box alone on the module that landed #2553.

`_var_consumer_chains` reads a vars file's reach from the tasks that consume the vars it
defines, and narrows only when EVERY var has a consumer -- the reject half below, because a
var resolved by textual search says nothing about the next one.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_vars_files.py
"""

from pathlib import Path

import pytest
import yaml

import land_reach

REPO_ROOT = Path(__file__).resolve().parents[3]

# PR #2553's `gitops_deploy` paths, verbatim (`gh pr view 2553 --json files`). The
# `defaults/main.yml` entry is the one that widened the note.
_PR_2553_PATHS = [
    "ansible/roles/setup/gitops_deploy/CLAUDE.md",
    "ansible/roles/setup/gitops_deploy/defaults/main.yml",
    "ansible/roles/setup/gitops_deploy/files/deploy_config.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_k8s.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_remediation.py",
    "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2",
    "ansible/roles/setup/gitops_deploy/tests/test_deploy_config.py",
]


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    missing = [p for p in _PR_2553_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_the_deployer_defaults_reach_the_gitops_host_only():
    """The accept half against the live tree. Every var `gitops_deploy/defaults/main.yml`
    defines is read by `install.yml` or by a template it ships, and `tasks/main.yml` includes
    that file under `when: has_gitops`."""
    assert land_reach.setup_file_hosts(
        "gitops_deploy", "ansible/roles/setup/gitops_deploy/defaults/main.yml"
    ) == frozenset({"daniel-box"})


def test_pr_2553_owes_no_host_beyond_the_tick():
    """The verdict PR #2553 should have read: `settled`, not `needs-manual-apply`."""
    assert land_reach.remaining_setup_hosts_note(_PR_2553_PATHS, "daniel-box") == ""


def test_pr_2553_from_another_host_still_names_the_gitops_host():
    """Per file, not a blanket silence: the same files DO reach daniel-box."""
    note = land_reach.remaining_setup_hosts_note(_PR_2553_PATHS, "daniel-server")
    assert "daniel-box" in note
    assert "daniel-pi" not in note


@pytest.fixture
def _knobs_role(tmp_path):
    """A gateless role whose vars are consumed on daniel-box alone, the dispatcher shape.

    `install.yml` comes in under `when: has_gitops` and holds both consumers: a template it
    ships, which reads `knobs_user`, and a cross-role `{{ role_path }}` import whose `vars:`
    passes `knobs_cron_hour`. `teardown.yml` is the other half and consumes neither.
    `vars/main.yml` repeats one of them beside a var nothing reads, which is the reject half.
    Synthetic, so the pair cannot drift with the live tree's gates.
    """
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "x", "roles": [{"role": "knobs"}]}]))
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(yaml.safe_dump({"has_gitops": False}))
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    (host_vars_dir / "daniel-box.yml").write_text(yaml.safe_dump({"has_gitops": True}))
    role_dir = tmp_path / "roles" / "knobs"
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
                },
                {
                    "name": "Install the shared timer",
                    "ansible.builtin.import_tasks": "{{ role_path }}/../common/tasks/"
                    "kuma_check_timer.yml",
                    "vars": {
                        "kuma_check_on_calendar": "*-*-* {{ knobs_cron_hour }}:00:00"
                    },
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
    (role_dir / "templates" / "widget.service.j2").write_text(
        "[Service]\nUser={{ knobs_user }}\n"
    )
    (role_dir / "defaults").mkdir()
    (role_dir / "defaults" / "main.yml").write_text(
        yaml.safe_dump({"knobs_user": "ubuntu", "knobs_cron_hour": 6})
    )
    (role_dir / "vars").mkdir()
    (role_dir / "vars" / "main.yml").write_text(
        yaml.safe_dump({"knobs_user": "ubuntu", "knobs_unread_knob": True})
    )
    return playbook, all_vars, host_vars_dir, tmp_path / "roles"


def _hosts(fixture, path):
    playbook, all_vars, host_vars_dir, roles_dir = fixture
    return land_reach.setup_file_hosts(
        "knobs",
        path,
        playbook=playbook,
        all_vars=all_vars,
        host_vars_dir=host_vars_dir,
        roles_dir=roles_dir,
    )


def test_a_defaults_file_narrows_to_the_hosts_its_consumers_run_on(_knobs_role):
    """The accept half. One var is read by a shipped template, the other only by the `vars:`
    of a cross-role import -- the shape `_gates_in` used to skip outright, which left that
    var with no consumer and kept the whole file wide."""
    assert _hosts(
        _knobs_role, "ansible/roles/setup/knobs/defaults/main.yml"
    ) == frozenset({"daniel-box"})


def test_a_vars_file_stays_wide_when_one_var_has_no_consumer(_knobs_role):
    """The reject half. `vars/main.yml` repeats the box-only var beside one nothing reads,
    and a var whose consumer this cannot find may be read through a shape it cannot see -- a
    `hostvars` lookup, a value the deployer's own Python reads at runtime. Narrower than the
    truth hides an unconverged host, so the file keeps the role-level answer."""
    assert _hosts(_knobs_role, "ansible/roles/setup/knobs/vars/main.yml") == frozenset(
        land_reach._HOSTS
    )


def test_a_vars_file_that_cannot_be_read_stays_wide(_knobs_role):
    """Unknown stays wide, the same asymmetry `_eval_when` applies inside one gate."""
    roles_dir = _knobs_role[3]
    (roles_dir / "knobs" / "defaults" / "main.yml").write_text("{{ not yaml")
    assert _hosts(
        _knobs_role, "ansible/roles/setup/knobs/defaults/main.yml"
    ) == frozenset(land_reach._HOSTS)
