#!/usr/bin/env python3
"""A `block: ... when:` at the top of a role's main.yml narrows the files it ships and the role's own reach.

A role whose main.yml is one `block:` under `when: has_gitops`, true on daniel-box alone, must
not prescribe `initial_setup.yml --tags <role>` on daniel-server and daniel-pi for a change to
its files. `_gates_in` recurses into `block:` bodies with the block's `when:` inherited, so a
file shipped inside the block reads as daniel-box only. A role's own `tests/*.py` beside them
must not fall through to the role-level reach.

A `tasks/<file>.yml` path that names no shipped file (the blocked role's `tasks/main.yml`,
`gitops_deploy/tasks/install.yml`) must not fall through to the ROLE-level reach, which for a
role with no playbook gate reads as every host however its own tasks are gated.
`setup_role_hosts` derives a gateless role's reach from its leaf tasks, so a block-gated role's
role-level answer is the block gate's answer.

No live setup role has the block shape since the deploy UI left for daniel-box's package
(H7.50), so the accept halves run over a synthetic tree. The reject half is a synthetic role
with one ungated leaf beside the gated one, plus the live `initial_setup` role.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_block_gate.py
"""

import pytest
import yaml

from deploy_tools.land_lib import land_reach

from lib.repo_paths import REPO as REPO_ROOT

# An ungated `copy` under a role initial_setup.yml applies everywhere. If this ever
# narrows, the narrowing lost its evidence.
_UNGATED = "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_UNGATED,)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_an_ungated_shipping_task_still_reaches_every_host():
    """The reject half, so a later narrowing of `_gates_in` cannot go quiet: no block, no
    when, no playbook gate -- all three hosts. The role-level read must agree: the live
    `initial_setup` role has ungated leaves, and a role-level answer that narrowed it would
    silently skip hosts that need an apply for every file it ships."""
    assert land_reach.setup_role_hosts("initial_setup") == frozenset(land_reach._HOSTS)
    assert land_reach.setup_file_hosts("initial_setup", _UNGATED) == frozenset(
        land_reach._HOSTS
    )


# A PR's setup-plane paths, verbatim (`git show --name-only` of its merge commit), minus the
# paths outside `ansible/roles/setup/` -- those derive tags or nothing and never reach this
# note -- the two `gitops_markers.py` copies #3275 deleted, and the deploy UI's paths, which
# H7.50 moved into a host package. The `tasks/` entry is one that widened it.
_PR_2071_SETUP_PATHS = [
    "ansible/roles/setup/gitops_deploy/CLAUDE.md",
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py",
    "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_state.py",
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
]


def test_pr_2071_paths_still_exist():
    """Non-vacuity for the cases below."""
    missing = [p for p in _PR_2071_SETUP_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_pr_2071_owes_no_host_beyond_the_tick():
    """The verdict is `settled`, not `needs-manual-apply`."""
    assert (
        land_reach.remaining_setup_hosts_note(_PR_2071_SETUP_PATHS, "daniel-box") == ""
    )


@pytest.fixture
def _gateless_roles(tmp_path):
    """Two roles with no playbook gate, told apart only by their own tasks: `blocked` is one
    `block:` under `when: has_gitops`; `open` has one ungated leaf beside
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


def _blocked_path(role: str, rel: str) -> str:
    return f"ansible/roles/setup/{role}/{rel}"


def test_a_file_shipped_inside_the_gated_block_reaches_the_gitops_host_only(
    _gateless_roles,
):
    """The accept half: the block's `when:` is inherited by the task inside it, while `open`'s
    ungated copy of the same file keeps every host."""
    playbook, all_vars, host_vars_dir, roles_dir = _gateless_roles
    args = (playbook, all_vars, host_vars_dir, roles_dir)
    assert land_reach.setup_file_hosts(
        "blocked", _blocked_path("blocked", "files/tool.py"), *args
    ) == frozenset({"daniel-box"})
    assert land_reach.setup_file_hosts(
        "open", _blocked_path("open", "files/tool.py"), *args
    ) == frozenset(land_reach._HOSTS)


def test_the_blocked_roles_own_tasks_file_reaches_the_gitops_host_only(_gateless_roles):
    """A path no shipping task names takes the role-level answer, which reads through the
    block; a test file beside the role ships nowhere."""
    playbook, all_vars, host_vars_dir, roles_dir = _gateless_roles
    args = (playbook, all_vars, host_vars_dir, roles_dir)
    assert land_reach.setup_file_hosts(
        "blocked", _blocked_path("blocked", "tasks/main.yml"), *args
    ) == frozenset({"daniel-box"})
    assert (
        land_reach.setup_file_hosts(
            "blocked", _blocked_path("blocked", "tests/test_tool.py"), *args
        )
        == frozenset()
    )
