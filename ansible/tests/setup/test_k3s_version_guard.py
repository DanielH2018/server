#!/usr/bin/env python3
"""Guards the k3s install tasks' `when:` now that SUC owns both nodes' versions.

SUC's `k3s-server` Plan has been the only writer of daniel-box's version since H6.3c, and its
`k3s-agent` Plan the only writer of daniel-server's since H6.3d. So the install task may not
mention `k3s_version` in its `when:`: a pin bump would make Ansible reinstall the binary SUC is
replacing, and the two writers would fight over it. The server's task runs for a missing binary alone
(a fresh host), and hosts/bootstrap-host.sh is the agent's install since H7.34.

Each test here encodes a way the guard regresses while everything still reads green:

  * The version clause returns. That makes a second writer of the version.

  * An args clause returns. The installer would then rewrite the unit's flags behind the
    package's back, a second writer of the node's flags.

  * The binary read loses `check_mode: false`. A `command` task is skipped under `--check`,
    which leaves its register undefined — and it is the guard consuming it two tasks later
    that fails, not the read. That is the same shape as the repo's
    check-mode-breaks-downstream-consumers class, and grepping for `retries:` would miss it.

Run: uv run pytest ansible/tests/setup/test_k3s_version_guard.py
"""

import pytest

from _helpers import load_tasks
from lib.repo_paths import K3S_ROLE


K3S_TASKS = K3S_ROLE / "tasks"

# (task file, install-task name, the register its binary read must fill)
INSTALLS = [
    ("server.yml", "Install the k3s server", "k3s_installed_version"),
]


def _named(filename: str, name: str) -> dict:
    for task in load_tasks(K3S_TASKS / filename):
        if task.get("name") == name:
            return task
    raise AssertionError(f"{filename} has no task named {name!r}")


def _version_reads(filename: str) -> list[dict]:
    """Tasks that run `k3s --version`, whatever they are called."""
    return [
        t
        for t in load_tasks(K3S_TASKS / filename)
        if "--version" in str(t.get("ansible.builtin.command", {}).get("cmd", ""))
    ]


@pytest.mark.parametrize(
    "filename,name,register,subcommand",
    [(*INSTALLS[0], "server")],
)
def test_the_install_passes_no_flags(filename, name, register, subcommand) -> None:
    """The package owns each node's flags, so the installer gets a bare subcommand."""
    task = _named(filename, name)
    assert task["environment"]["INSTALL_K3S_EXEC"] == subcommand
    assert str(task["when"]) == f"({register}.rc | default(1)) != 0"


@pytest.mark.parametrize("filename,name,register", INSTALLS)
def test_install_leaves_the_version_to_suc(filename, name, register) -> None:
    """The version clause is gone, and a missing binary still runs the install."""
    when = str(_named(filename, name).get("when", ""))
    assert "k3s_version" not in when, (
        f"{filename}: SUC owns the version; the guard must not"
    )
    assert register in when, f"{filename}: a missing binary must still run the install"


def test_version_read_runs_in_check_mode() -> None:
    """`--check` must not leave the register undefined.

    A `command` task is skipped under check mode, and the failure then surfaces at the guard
    that consumes the register, not at the read — so this is invisible until a --check run
    dies pointing at the wrong task.
    """
    for filename, _, _ in INSTALLS:
        reads = _version_reads(filename)
        assert reads, f"{filename}: nothing runs `k3s --version` any more"
        for task in reads:
            assert task.get("check_mode") is False, (
                f"{filename}: the `k3s --version` read needs `check_mode: false`, or a "
                f"--check run skips it and the install guard fails on an undefined register."
            )


def test_version_read_is_non_fatal_and_idempotent() -> None:
    """A missing binary means "install it", not "fail the play".

    On a fresh host /usr/local/bin/k3s does not exist yet. The read must not fail the play,
    and must not report changed — it only reads.
    """
    for filename, _, _ in INSTALLS:
        for task in _version_reads(filename):
            assert task.get("failed_when") is False, (
                f"{filename}: the `k3s --version` read must not fail the play on a host "
                f"where k3s is not installed yet."
            )
            assert task.get("changed_when") is False, (
                f"{filename}: reading a version is not a change."
            )
