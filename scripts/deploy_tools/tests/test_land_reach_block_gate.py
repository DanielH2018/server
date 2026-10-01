#!/usr/bin/env python3
"""A `block: ... when:` at the top of a role's main.yml narrows the files it ships and the role's own reach.

deploy_ui's main.yml is one `block:` under `when: has_gitops`, true on daniel-box alone, so a
change to its files must not prescribe `initial_setup.yml --tags deploy_ui` on daniel-server and
daniel-pi. `_gates_in` recurses into `block:` bodies with the block's `when:` inherited, so
`files/deploy_ui.py` reads as daniel-box only. A role's own `tests/*.py` beside them must not
fall through to the role-level reach.

The accept half is deploy_ui's own files (box only, through the block gate) and the note over
the role's whole file list (empty); the reject half is an ungated shipping task in a role with
no playbook gate, which must keep reaching every host.

A `tasks/<file>.yml` path that names no shipped file (`deploy_ui/tasks/main.yml`,
`gitops_deploy/tasks/install.yml`) must not fall through to the ROLE-level reach, which for a
role with no playbook gate reads as every host however its own tasks are gated.
`setup_role_hosts` derives a gateless role's reach from its leaf tasks, so `deploy_ui`'s
role-level answer is the block gate's answer, and a `tasks/<file>.yml` path reaches the hosts
that run a task IN that file. The reject half is a synthetic role with one ungated leaf, which
must keep every host, beside the gated one that must not.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_block_gate.py
"""

import pytest
import yaml

import land_reach
from lib import yaml_fast

from lib.repo_paths import REPO as REPO_ROOT

_ROLE = "deploy_ui"
_MAIN = "ansible/roles/setup/deploy_ui/tasks/main.yml"
_SHIPPED = (
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.html",
    "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2",
)
# An ungated `copy` under a role initial_setup.yml applies everywhere. If this ever
# narrows, the narrowing lost its evidence.
_UNGATED = "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh"
# A real PR's file list for `deploy_ui`, verbatim.
_PR_1901_PATHS = [
    "ansible/roles/setup/deploy_ui/CLAUDE.md",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.html",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_writes.py",
    "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_app.py",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_writes.py",
    "ansible/tests/deploy/test_inventory_block_scalars_have_no_comment_shaped_lines.py",
    "docs/deploying.md",
    "scripts/deploy_tools/narrow_broad.py",
]


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_MAIN, _UNGATED, *_SHIPPED, *_PR_1901_PATHS)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_deploy_ui_is_still_one_block_gated_on_has_gitops():
    """The shape this file pins: every top-level task is a `block:` carrying `when:
    has_gitops`, and the role itself has no playbook gate (so the block is the ONLY thing
    keeping its files -- and the role -- off the other hosts)."""
    tasks = yaml_fast.safe_load((REPO_ROOT / _MAIN).read_text())
    assert tasks and all(
        "block" in t and t.get("when") == "has_gitops" for t in tasks
    ), [t.get("name") for t in tasks]
    assert land_reach._initial_setup_roles()[_ROLE] is None
    assert land_reach.setup_role_hosts(_ROLE) == frozenset({"daniel-box"})


def test_a_file_shipped_inside_the_gated_block_reaches_the_gitops_host_only():
    """The accept half: the block's `when:` is inherited by the tasks inside it, the copy
    loop and the template alike."""
    for path in _SHIPPED:
        assert land_reach.setup_file_hosts(_ROLE, path) == frozenset({"daniel-box"}), (
            path
        )


def test_an_ungated_shipping_task_still_reaches_every_host():
    """The reject half, so a later narrowing of `_gates_in` cannot go quiet: no block, no
    when, no playbook gate -- all three hosts. The role-level read must agree: the live
    `initial_setup` role has ungated leaves, and a role-level answer that narrowed it would
    silently skip hosts that need an apply for every file it ships."""
    assert land_reach.setup_role_hosts("initial_setup") == frozenset(land_reach._HOSTS)
    assert land_reach.setup_file_hosts("initial_setup", _UNGATED) == frozenset(
        land_reach._HOSTS
    )


def test_pr_1901_owes_no_host_beyond_the_tick():
    """The verdict is `settled`."""
    assert land_reach.remaining_setup_hosts_note(_PR_1901_PATHS, "daniel-box") == ""


def test_pr_1901_from_another_host_still_names_the_gitops_host():
    """Per file, not a blanket silence: the same files DO reach daniel-box."""
    note = land_reach.remaining_setup_hosts_note(_PR_1901_PATHS, "daniel-server")
    assert "daniel-box" in note
    assert "daniel-pi" not in note


# A PR's setup-plane paths, verbatim (`git show --name-only` of its merge commit), minus the
# paths outside `ansible/roles/setup/` -- those derive tags or nothing and never reach this
# note. The two `tasks/` entries are the ones that widened it.
_PR_2071_SETUP_PATHS = [
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_writes.py",
    "ansible/roles/setup/deploy_ui/files/gitops_markers.py",
    "ansible/roles/setup/deploy_ui/tasks/main.yml",
    "ansible/roles/setup/gitops_deploy/CLAUDE.md",
    "ansible/roles/setup/gitops_deploy/files/deploy_remediation.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_state.py",
    "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
    "ansible/roles/setup/gitops_deploy/files/gitops_markers.py",
    "ansible/roles/setup/gitops_deploy/tasks/install.yml",
    "ansible/roles/setup/gitops_deploy/tests/conftest.py",
    "ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_channels.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_delivery.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_failure_output.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_not_the_deployer.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_markers.py",
    "ansible/roles/setup/renovate_agent/files/gitops_markers.py",
    "ansible/roles/setup/renovate_agent/files/renovate_agent.py",
    "ansible/roles/setup/renovate_agent/tasks/main.yml",
]


def test_pr_2071_paths_still_exist():
    """Non-vacuity for the cases below."""
    missing = [p for p in _PR_2071_SETUP_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_deploy_ui_paths_outside_the_shipped_dirs_reach_the_gitops_host_only():
    """The accept half at role level: a path no shipping task names -- the tasks file
    itself, the defaults -- takes the role-level answer, which reads through the block."""
    for path in (_MAIN, "ansible/roles/setup/deploy_ui/defaults/main.yml"):
        assert land_reach.setup_file_hosts(_ROLE, path) == frozenset({"daniel-box"}), (
            path
        )


def test_pr_2071_owes_no_host_beyond_the_tick():
    """The verdict is `settled`, not `needs-manual-apply`."""
    assert (
        land_reach.remaining_setup_hosts_note(_PR_2071_SETUP_PATHS, "daniel-box") == ""
    )


@pytest.fixture
def _gateless_roles(tmp_path):
    """Two roles with no playbook gate, told apart only by their own tasks: `blocked` is one
    `block:` under `when: has_gitops` (deploy_ui's shape); `open` has one ungated leaf beside
    a gated one. Synthetic, so the pair cannot drift with the live tree's gates."""
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(
        yaml.safe_dump(
            [{"hosts": "x", "roles": [{"role": "blocked"}, {"role": "open"}]}]
        )
    )
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(yaml.safe_dump({"has_gitops": False}))
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    (host_vars_dir / "daniel-box.yml").write_text(yaml.safe_dump({"has_gitops": True}))
    roles_dir = tmp_path / "roles"
    copy = {"ansible.builtin.copy": {"src": "tool.py", "dest": "/opt/tool.py"}}
    for role, tasks in (
        ("blocked", [{"name": "install", "when": "has_gitops", "block": [copy]}]),
        ("open", [dict(copy, when="has_gitops"), {"name": "everywhere", **copy}]),
    ):
        (roles_dir / role / "tasks").mkdir(parents=True)
        (roles_dir / role / "tasks" / "main.yml").write_text(yaml.safe_dump(tasks))
    return playbook, all_vars, host_vars_dir, roles_dir


def test_a_gateless_role_reaches_where_one_of_its_leaf_tasks_runs(_gateless_roles):
    """Accept and reject halves of the role-level read, side by side: the block gate narrows
    `blocked` to daniel-box, and `open`'s one ungated leaf keeps every host. If the reject
    half ever narrows, the derivation lost its evidence."""
    playbook, all_vars, host_vars_dir, roles_dir = _gateless_roles
    assert land_reach.setup_role_hosts(
        "blocked", playbook, all_vars, host_vars_dir, roles_dir
    ) == frozenset({"daniel-box"})
    assert land_reach.setup_role_hosts(
        "open", playbook, all_vars, host_vars_dir, roles_dir
    ) == frozenset(land_reach._HOSTS)


def test_a_gateless_role_with_no_readable_tasks_stays_wide(_gateless_roles):
    """Unknown stays wide: a role whose tasks/main.yml cannot be read is not narrowed."""
    playbook, all_vars, host_vars_dir, roles_dir = _gateless_roles
    (roles_dir / "blocked" / "tasks" / "main.yml").unlink()
    assert land_reach.setup_role_hosts(
        "blocked", playbook, all_vars, host_vars_dir, roles_dir
    ) == frozenset(land_reach._HOSTS)
