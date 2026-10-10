"""The worktree sweep's root helper is installed where the sudoers rule and the caller look.

`lib.worktrees.privileged_holders` checks execute permission on `WORKTREE_HOLDERS` before it
asks sudo, and the sudoers rule names the same path. If the three drift apart, the sweep
either falls back to the unprivileged scan without saying why, or sudo refuses it every run.

Run: uv run pytest ansible/tests/setup/test_worktree_holders_install.py
"""

import re
from pathlib import PurePosixPath

import pytest
from _helpers import SETUP_ROLES, load_tasks, task_named
from lib.worktrees import WORKTREE_HOLDERS

CRONS = SETUP_ROLES / "initial_setup" / "tasks" / "crons.yml"


def _sudoers_command(content: str) -> str:
    match = re.search(r"NOPASSWD: (\S+) \"\"$", content, re.MULTILINE)
    assert match, f"no argument-free NOPASSWD rule in:\n{content}"
    return match.group(1)


def test_install_dest_sudoers_rule_and_caller_name_one_path_is_clean():
    tasks = load_tasks(CRONS)
    install = task_named(tasks, "Install the root-run worktree holder scan")
    rule = task_named(tasks, "Allow sys_user to run the worktree holder scan")

    assert install["ansible.builtin.copy"]["dest"] == WORKTREE_HOLDERS
    assert _sudoers_command(rule["ansible.builtin.copy"]["content"]) == WORKTREE_HOLDERS


def _parent_created_before_install(tasks: list[dict], dest: str) -> bool:
    parent = str(PurePosixPath(dest).parent)
    for task in tasks:
        if task.get("ansible.builtin.copy", {}).get("dest") == dest:
            return False
        made = task.get("ansible.builtin.file", {})
        if made.get("path") == parent and made.get("state") == "directory":
            return True
    raise AssertionError(f"no task installs {dest}")


def test_the_install_directory_is_created_before_the_copy_is_clean():
    # `copy` does not create a missing parent, and Ubuntu ships no /usr/local/libexec. The first
    # apply failed on daniel-box for exactly that and held the initial_setup plane.
    assert _parent_created_before_install(load_tasks(CRONS), WORKTREE_HOLDERS)


def test_an_install_with_no_directory_task_before_it_is_flagged():
    install = {"ansible.builtin.copy": {"dest": WORKTREE_HOLDERS}}
    late_dir = {
        "ansible.builtin.file": {
            "path": str(PurePosixPath(WORKTREE_HOLDERS).parent),
            "state": "directory",
        }
    }
    assert not _parent_created_before_install([install, late_dir], WORKTREE_HOLDERS)


def test_a_rule_that_accepts_arguments_is_flagged():
    # Without `""`, sudo lets the caller pass any argument, and a later helper that read one
    # would let sys_user ask root about any directory.
    with pytest.raises(AssertionError, match="no argument-free NOPASSWD rule"):
        _sudoers_command(
            "ubuntu ALL=(root) NOPASSWD: /usr/local/libexec/worktree-holders\n"
        )
