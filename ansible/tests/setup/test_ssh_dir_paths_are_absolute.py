"""The SSH-directory tasks name the directory they mean, and no role task leaves `~` to sudo.

`Harden the sys user's SSH directory` read `path: ~/.ssh` with no `become:` of its own, under a
play that becomes root. Ansible's file module expands `~` as the become user and the sudo plugin
passes `-H`, so the task chowned /root/.ssh to ubuntu recursively on every run and never touched
/home/ubuntu/.ssh — while reporting `ok` both times (daniel-box, 2026-09-23 and 2026-09-24).
Nothing in the task's own text says which directory it writes, which is why the bug survived
every reading of it: the answer is in the play's `become:` and in sudo's default flags.

The first two tests pin the fix at the two tasks. The last pair is the class guard — a
home-relative path in a role task is never right here, because every playbook in this repo
becomes root and none of them means /root. It is a ratchet: there are no hits today.

Run: uv run pytest ansible/tests/setup/test_ssh_dir_paths_are_absolute.py
"""

from pathlib import Path

from _helpers import ROLES
from _helpers import load_tasks
from _helpers import task_named
from _helpers import walk_tasks

ACCESS = ROLES / "setup" / "initial_setup" / "tasks" / "access.yml"

# The keys that name a path on the target host. `src:` is deliberately absent: for `copy:` and
# `template:` it names a file in the CONTROL node's role, where `~` never reaches sudo.
_PATH_KEYS = ("path", "dest")


def _home_relative_paths(task: dict) -> list[str]:
    """Every `~`-relative target this task writes, across whichever module it uses.

    Takes the task dict rather than a file, so the rejecting test below can hand it a synthetic
    task — the repo scan alone would pass just as well if this stopped matching anything.
    """
    found = []
    for key, value in task.items():
        if not isinstance(value, dict):
            continue
        for path_key in _PATH_KEYS:
            target = value.get(path_key)
            if isinstance(target, str) and target.startswith("~"):
                found.append(f"{key}.{path_key}={target}")
    return found


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


def test_a_home_relative_path_is_flagged() -> None:
    """FLAGGED half: the exact shape #2413 shipped, so the scan below cannot go inert."""
    assert _home_relative_paths(
        {
            "name": "Set SSH directory permissions",
            "ansible.builtin.file": {"path": "~/.ssh"},
        }
    ) == ["ansible.builtin.file.path=~/.ssh"]
    assert (
        _home_relative_paths(
            {
                "name": "Harden",
                "ansible.builtin.file": {"path": "/home/{{ sys_user }}/.ssh"},
            }
        )
        == []
    )


def test_no_role_task_writes_a_home_relative_path() -> None:
    """CLEAN half: `~` in a role task resolves to whoever the play became, not to who you meant."""
    task_files = sorted(ROLES.glob("*/*/tasks/*.yml"))
    # Non-vacuity: a glob that stops matching returns an empty set, and `not offenders` over
    # nothing passes. 100 is well under the count today and well over anything a reorganisation
    # would plausibly leave behind.
    assert len(task_files) >= 100, f"the task-file glob found only {len(task_files)}"
    offenders: list[str] = []
    for path in task_files:
        for task in walk_tasks(load_tasks(path)):
            for hit in _home_relative_paths(task):
                offenders.append(
                    f"{Path(path).relative_to(ROLES)}: {task.get('name')}: {hit}"
                )
    assert not offenders, "home-relative target(s) in a role task:\n" + "\n".join(
        offenders
    )
