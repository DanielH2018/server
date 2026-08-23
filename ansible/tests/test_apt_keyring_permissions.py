#!/usr/bin/env python3
"""Guards against APT keyrings being created without an explicit mode.

The failure this encodes actually happened (daniel-box, 2026-08-01). The
initial_setup role sets UMASK 027 in /etc/login.defs, and docker_install — which
runs later in the SAME play — created its keyring with `gpg --dearmor` via
ansible.builtin.command. `command` has no `mode:`, so the file inherited root's
now-restrictive umask and landed 0640. apt fetches as the unprivileged `_apt`
user, could not read the keyring, and reported the repo as unsigned:

    GPG error: https://download.docker.com/linux/ubuntu noble InRelease:
    NO_PUBKEY 7EA0A9C3F273FCD8 ... repository is not signed

That failed initial_setup.yml at the cache refresh, one task before Docker would
have been installed. Hosts provisioned before the umask change never showed it:
their keyring was already 0644 and a `creates:` guard stopped it being rewritten.

Both rules below are about the same thing — a keyring's mode must be stated, not
inherited from whatever the ambient umask happens to be.

Run: uv run pytest ansible/tests/test_apt_keyring_permissions.py
"""

import pytest

from _helpers import SETUP_ROLES, load_tasks, walk_tasks

KEYRING_DIRS = ("/etc/apt/keyrings", "/usr/share/keyrings")

# Modules that write a file and accept `mode:`.
FILE_WRITING_MODULES = (
    "ansible.builtin.get_url",
    "ansible.builtin.copy",
    "ansible.builtin.template",
)

COMMAND_MODULES = ("ansible.builtin.command", "ansible.builtin.shell")


def _task_files():
    return sorted(SETUP_ROLES.glob("*/tasks/*.yml"))


def _all_tasks():
    for path in _task_files():
        for task in walk_tasks(load_tasks(path)):
            yield path, task


def _rel(path):
    return path.relative_to(SETUP_ROLES.parents[2])


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_no_command_dearmors_a_keyring(path):
    # `gpg --dearmor` via command/shell is the exact shape that inherits the umask.
    # apt reads ASCII-armored keys referenced by Signed-By, so fetch the .asc directly
    # with get_url and an explicit mode instead — see docker_install/tasks/install.yml.
    offenders = []
    for task in walk_tasks(load_tasks(path)):
        for module in COMMAND_MODULES:
            spec = task.get(module)
            if not spec:
                continue
            cmd = spec if isinstance(spec, str) else str(spec.get("cmd", ""))
            args_creates = str((task.get("args") or {}).get("creates", ""))
            if "--dearmor" in cmd:
                offenders.append(
                    f"{task.get('name', '<unnamed>')}: {cmd} {args_creates}".strip()
                )

    assert not offenders, (
        f"{_rel(path)} creates an APT keyring with `gpg --dearmor` via command/shell. "
        "That file inherits root's umask (initial_setup sets 027), so it can land 0640 and "
        "apt's unprivileged _apt user will treat the repo as unsigned. Fetch the armored key "
        f"with get_url and an explicit mode instead. Offending: {offenders}"
    )


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_keyring_writes_set_an_explicit_mode(path):
    offenders = []
    for task in walk_tasks(load_tasks(path)):
        for module in FILE_WRITING_MODULES:
            spec = task.get(module)
            if not isinstance(spec, dict):
                continue
            dest = str(spec.get("dest") or spec.get("path") or "")
            if any(dest.startswith(d) for d in KEYRING_DIRS) and "mode" not in spec:
                offenders.append(f"{task.get('name', '<unnamed>')} -> {dest}")

    assert not offenders, (
        f"{_rel(path)} writes an APT keyring without an explicit `mode:`, so its permissions "
        "depend on the ambient umask. apt reads keyrings as the unprivileged _apt user and "
        f'silently treats an unreadable one as missing. Set mode: "0644". Offending: {offenders}'
    )


def test_the_guard_actually_inspects_something():
    # A path typo would make both tests above vacuously pass on zero files.
    tasks = list(_all_tasks())
    assert len(_task_files()) >= 5, (
        "expected several setup-role task files; check SETUP_ROLES"
    )
    assert tasks, "no tasks parsed — the guards above would pass vacuously"
