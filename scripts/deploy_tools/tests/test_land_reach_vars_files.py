#!/usr/bin/env python3
"""A `defaults/`/`vars/` change reaches the hosts that run the tasks consuming its vars.

`gitops_deploy` runs `teardown.yml` alone on daniel-server and daniel-pi, where `has_gitops` is
false, so an `initial_setup.yml --tags gitops_deploy` run there re-runs an idempotent teardown
and applies none of a change to the role's `defaults/main.yml`. No task names a defaults file,
so `setup_file_hosts` would fall through to the role-level reach, which for this dispatcher is
the union of both halves -- every host. Every `gitops_deploy/files/*.py` path reads as
daniel-box alone.

`_var_consumer_chains` reads a vars file's reach from the tasks that consume the vars it
defines, and narrows only when EVERY var has a consumer -- the reject half below, because a
var resolved by textual search says nothing about the next one.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_vars_files.py
"""

import pytest
import yaml

from deployer_lib import deploy_cross_role
from deploy_tools.land_lib import land_reach

from _narrow_fixtures import _refs
from _setup_role_fixtures import ROLE, build

from lib.repo_paths import REPO as REPO_ROOT

# The `gitops_deploy` paths of a real PR (`gh pr view <n> --json files`). The
# `defaults/main.yml` entry is the one that widened the note. D6.12 deleted the PR's
# `deploy_k8s.py`, so `deploy_phases.py`, a module beside it, stands in for it.
_PR_2553_PATHS = [
    "ansible/roles/setup/gitops_deploy/CLAUDE.md",
    "ansible/roles/setup/gitops_deploy/defaults/main.yml",
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_config.py",
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_phases.py",
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py",
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
    """The verdict is `settled`, not `needs-manual-apply`."""
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
    of a cross-role import -- the shape `_gates_in` must not skip, or that
    var has no consumer and the whole file stays wide."""
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


# `initial_setup/defaults/main.yml` names claude_code for every edit (#4303), and claude_code
# reads one of its keys. Two real ranges over it: 1e7eed71e bumped the uv pin, which
# claude_code never reads, and abf8d0d4a^..acb3513b0 added the socket group key it does read.
_INITIAL_SETUP_DEFAULTS = ["ansible/roles/setup/initial_setup/defaults/main.yml"]


def test_an_initial_setup_default_claude_code_never_reads_owes_it_no_host():
    """The tick drops claude_code over this range (#4361), so no hand-run of it is owed."""
    note = land_reach.remaining_setup_hosts_note(
        _INITIAL_SETUP_DEFAULTS, "daniel-box", pr_range="1e7eed71e^..1e7eed71e"
    )
    assert "`initial_setup` also reaches daniel-server" in note
    assert "`claude_code`" not in note


def test_an_initial_setup_default_claude_code_reads_still_names_its_other_host():
    """The other half: a key claude_code reads keeps the table's per-file reach."""
    note = land_reach.remaining_setup_hosts_note(
        _INITIAL_SETUP_DEFAULTS, "daniel-box", pr_range="abf8d0d4a^..acb3513b0"
    )
    assert "`claude_code` also reaches daniel-server" in note


# The pair above reaches the note through the live tree, where the read-a-key half refuses
# (`tasks/agent_access.yml` is not statically imported), so it never reaches the branch where
# the derivation answers with tags (#4411). This synthetic tree does: `demo` statically
# renders a key of `owner`'s defaults, the shape of `test_narrow_setup_vars_walk.py`'s
# `reads` fixture, so `foreign_key_tags` returns `alpha` for one key and nothing for the
# other.
_FOREIGN = "ansible/roles/setup/owner/defaults/main.yml"
_FOREIGN_DEFAULTS = "---\nowner_group: holders\nowner_other: unread\n"


@pytest.fixture
def _reads_tree(tmp_path, monkeypatch):
    """`demo` and `owner` in a gateless `initial_setup.yml`, with demo declared a reader."""
    monkeypatch.setitem(
        deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
        _FOREIGN,
        frozenset({"demo"}),
    )
    tree = build(tmp_path)
    tree.write(
        "ansible/initial_setup.yml",
        yaml.safe_dump(
            [
                {
                    "name": "Setup",
                    "hosts": "all",
                    "roles": [
                        {"role": "owner", "tags": ["owner"]},
                        {"role": "demo", "tags": ["demo"]},
                    ],
                }
            ]
        ),
    )
    tree.write("ansible/inventory/group_vars/all.yml", "---\nhas_gitops: false\n")
    tree.write("ansible/inventory/host_vars/daniel-box.yml", "---\nhas_gitops: true\n")
    tree.write(
        "ansible/roles/setup/owner/tasks/main.yml",
        "---\n- name: Own the group\n  ansible.builtin.group:\n"
        "    name: '{{ owner_group }}'\n  tags: [owner-group]\n",
    )
    tree.write(_FOREIGN, _FOREIGN_DEFAULTS)
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2",
        "mode = {{ demo_alpha_mode }}\ngroup = {{ owner_group }}\n",
    )
    tree.commit("demo reads the owner's group")
    return tree


def _note_over(tree, old_value, new_value):
    tree.write(_FOREIGN, _FOREIGN_DEFAULTS.replace(old_value, new_value))
    old, new = _refs(tree)
    return land_reach.remaining_setup_hosts_note(
        [_FOREIGN], "daniel-box", pr_range=f"{old}..{new}", repo=tree.root
    )


def test_a_foreign_key_the_role_reads_keeps_its_hosts_in_the_note(_reads_tree):
    """The unsafe direction: dropping demo here omits a host that owes a hand-run."""
    assert "`demo` also reaches daniel-server" in _note_over(
        _reads_tree, "holders", "renamed"
    )


def test_a_foreign_key_the_role_never_reads_drops_it_from_the_note(_reads_tree):
    """The other half, over the same tree, so the pair isolates the key read."""
    assert "`demo`" not in _note_over(_reads_tree, "unread", "changed")
