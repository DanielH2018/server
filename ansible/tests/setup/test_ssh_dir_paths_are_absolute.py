"""The SSH-directory tasks name the directory they mean, and no role task leaves `~` to sudo.

A task such as `Harden the sys user's SSH directory` that names `path: ~/.ssh` with no
`become:` of its own, under a play that becomes root, writes to /root/.ssh rather than
/home/ubuntu/.ssh: Ansible's file module expands `~` as the become user and the sudo plugin
passes `-H`. The task chowns /root/.ssh to ubuntu recursively on every run and never touches
/home/ubuntu/.ssh, while reporting `ok`. Nothing in the task's own text says which directory
it writes, which is why the bug survives every reading of it: the answer is in the play's
`become:` and in sudo's default flags.

The two tests here pin the fix at the two tasks. The class guard — no role task writes a
home-relative path, because every playbook in this repo becomes root and none of them means
/root — is the `role-tasks-write-no-home-relative-path` row of
`ansible/tests/repo/test_census_rows_roles.py` (#3430).

Run: uv run pytest ansible/tests/setup/test_ssh_dir_paths_are_absolute.py
"""

from _helpers import ROLES
from _helpers import load_tasks
from _helpers import task_named

ACCESS = ROLES / "setup" / "initial_setup" / "tasks" / "access.yml"


def test_the_sys_user_ssh_directory_is_named_absolutely() -> None:
    """The task hardens the sys user's directory, whoever the play becomes."""
    task = task_named(load_tasks(ACCESS), "Harden the sys user's SSH directory")
    module = task["ansible.builtin.file"]
    assert module["path"] == "/home/{{ sys_user }}/.ssh"
    assert module["owner"] == "{{ sys_user }}"
    assert module["group"] == "{{ sys_user }}"
    # `state: directory` is what makes `recurse: true` do anything at all — the module honours
    # recursion only for a directory, and inferring the state from disk leaves the task's
    # behaviour depending on what is already there.
    assert module["state"] == "directory"
    assert module["recurse"] is True


def test_roots_ssh_directory_is_given_back_and_never_created() -> None:
    """The cleanup half: the ownership the bug left on disk is corrected, but nothing is made."""
    tasks = load_tasks(ACCESS)
    restore = task_named(tasks, "Give root back its own SSH directory")
    module = restore["ansible.builtin.file"]
    assert module["path"] == "/root/.ssh"
    assert module["owner"] == "root"
    assert module["group"] == "root"
    # Modes are not in scope: the bug changed ownership only, and a recursive mode here would be
    # this task inventing a policy for a directory it is merely repairing.
    assert "mode" not in module
    stat = task_named(tasks, "Check whether root has an SSH directory")
    assert stat["ansible.builtin.stat"]["path"] == "/root/.ssh"
    registered = stat["register"]
    assert restore["when"] == f"{registered}.stat.isdir | default(false)"
