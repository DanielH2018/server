#!/usr/bin/env python3
"""A repo file a setup role ships from the checkout reaches that role's hosts.

`remaining_setup_hosts_note` must hand `setup_file_hosts` more than the paths under the role's
own directory: a repo file a task ships from the checkout reaches the hosts that run that task.
No live setup role ships a checkout file by a `{{ playbook_dir }}/../` literal on a gated task
since H7.52, so the cases run over a synthetic tree whose `shipper` role copies
`hosts/lib/ci_verdict.py` on daniel-box alone, and a landing on another host owes daniel-box the
apply.

The reject half is `scripts/dev/pytest_shard_weights.json`: no task ships it, so it must
contribute nothing. Without that half the fix would "pass" by widening every changed role to
every host it reaches, which is the failure `setup_repo_file_hosts` returns the empty set on
absent evidence to avoid.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_repo_shipped_files.py
"""

import pytest
import yaml

from deployer_lib import deploy_cross_role
from deploy_tools.land_lib import land_reach

from lib.repo_paths import REPO as REPO_ROOT
from deploy_tools.setup_lib.setup_role_chains import task_gates_shipping_repo_path

_ROLE = "shipper"
_SHIPPED = "hosts/lib/ci_verdict.py"
_NOT_SHIPPED = "scripts/dev/pytest_shard_weights.json"


@pytest.fixture
def shipper_tree(tmp_path):
    """A playbook running `shipper` (box-only copy of `_SHIPPED`) and `bystander` (ships nothing)."""
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(
        yaml.safe_dump(
            [{"hosts": "x", "roles": [{"role": _ROLE}, {"role": "bystander"}]}]
        )
    )
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("{}\n")
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    roles_dir = tmp_path / "roles"
    for name, tasks in {
        _ROLE: [
            {
                "name": "Install the verdict reader",
                "ansible.builtin.copy": {
                    "src": "{{ playbook_dir }}/../" + _SHIPPED,
                    "dest": "/usr/local/lib/ci_verdict.py",
                },
                "when": "inventory_hostname == 'daniel-box'",
            }
        ],
        "bystander": [{"name": "Noop", "ansible.builtin.debug": {"msg": "x"}}],
    }.items():
        (roles_dir / name / "tasks").mkdir(parents=True)
        (roles_dir / name / "tasks" / "main.yml").write_text(yaml.safe_dump(tasks))
    return playbook, all_vars, host_vars_dir, roles_dir


def _hosts(tree, role, path):
    return land_reach.setup_repo_file_hosts(role, path, *tree)


def _note(tree, files, local_host):
    return land_reach._remaining_note(
        *land_reach._remaining_hosts(files, local_host, (), *tree), local_host
    )


def test_the_shipped_and_unshipped_paths_still_exist():
    """Non-vacuity: a renamed file makes every case below read green over a path nobody uses."""
    assert (REPO_ROOT / _SHIPPED).exists()
    assert (REPO_ROOT / _NOT_SHIPPED).exists()


def test_a_shipped_repo_file_reaches_the_installing_host(shipper_tree):
    """The accept half: only daniel-box runs the copy, so only it owes the apply."""
    assert _hosts(shipper_tree, _ROLE, _SHIPPED) == frozenset({"daniel-box"})


def test_a_repo_file_no_task_ships_reaches_no_host(shipper_tree):
    """The reject half, and the one that keeps the accept half honest."""
    assert _hosts(shipper_tree, _ROLE, _NOT_SHIPPED) == frozenset()


def test_another_role_does_not_claim_a_file_it_does_not_ship(shipper_tree):
    """Evidence is per role: `bystander` ships no CI verdict reader."""
    assert _hosts(shipper_tree, "bystander", _SHIPPED) == frozenset()


# --- A PR that puts no role in the change set at all -------------------------
#
# These cases adopt a deployer table without a row for `_SHIPPED`, so a PR changing it alone puts
# no role in the change set. The arm they hold is the backstop for a checkout copy the table lacks, such as one whose `src:` is
# templated and so escapes `test_setup_cross_role_files.py`'s census.


@pytest.fixture
def table_without_the_row():
    live = deploy_cross_role.current_tables()
    shipped = {
        p: r
        for p, r in deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES.items()
        if p != _SHIPPED
    }
    deploy_cross_role.use_tables(
        {**live, "SETUP_FILES_SHIPPED_BY_OTHER_ROLES": shipped}
    )
    yield
    deploy_cross_role.use_tables(live)


def test_a_repo_file_only_pr_names_the_installing_host(
    shipper_tree, table_without_the_row
):
    """The accept half: no path under the role's directory at all.

    `cs.setup_roles` is built by path classification, so this PR leaves it empty and the
    self-applied loop has nothing to iterate. The role is reached through the file it ships.
    """
    note = _note(shipper_tree, [_SHIPPED], "daniel-server")
    assert "daniel-box" in note, note
    assert "ships a changed repo file" in note, note


def test_a_repo_file_only_pr_of_a_file_no_task_ships_names_nobody(shipper_tree):
    """The reject half: widening every setup role to its reach is the failure to avoid."""
    assert _note(shipper_tree, [_NOT_SHIPPED], "daniel-box") == ""


def test_the_ticks_own_host_is_owed_when_the_role_entered_by_the_file_alone(
    shipper_tree, table_without_the_row
):
    """No `- {local_host}` here: with no role path in the PR the tick applied the role nowhere.

    Landing the same PR ON daniel-box must still name daniel-box, and with the plain local
    command rather than an ssh hop to the machine already running it.
    """
    note = _note(shipper_tree, [_SHIPPED], "daniel-box")
    assert "daniel-box" in note, note
    assert "ssh daniel-box" not in note, note
    assert (
        f"`uv run ansible-playbook ansible/initial_setup.yml --tags {_ROLE}`" in note
    ), note


# --- A task NAME that mentions a repo path ships nothing (#6538) -------------
#
# The retired sops_setup role named a seed task after `ansible/.sops.yaml` and wrote the file
# from inline `content:`. A match over the whole task read the name as the ship site, so a range
# changing only the SOPS config put the role in the unapplied half on every host.

_SOPS_CONFIG = "ansible/.sops.yaml"


def _role(tmp_path, task: str):
    tasks = tmp_path / "demo" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(task)
    return tmp_path / "demo"


def test_a_task_name_mentioning_a_repo_path_does_not_ship_it(tmp_path):
    role = _role(
        tmp_path,
        f"- name: Seed {_SOPS_CONFIG} (first host only)\n"
        "  ansible.builtin.copy:\n    dest: /tmp/x\n    content: x\n",
    )
    assert not task_gates_shipping_repo_path(role, _SOPS_CONFIG)


def test_a_task_that_copies_the_repo_path_ships_it(tmp_path):
    role = _role(
        tmp_path,
        "- name: Seed the config\n"
        f"  ansible.builtin.copy:\n    src: {_SOPS_CONFIG}\n    dest: /tmp/x\n",
    )
    assert task_gates_shipping_repo_path(role, _SOPS_CONFIG)
