#!/usr/bin/env python3
"""Guards the k3s install tasks' `when:` now that SUC owns both nodes' versions.

SUC's `k3s-server` Plan has been the only writer of daniel-box's version since H6.3c, and its
`k3s-agent` Plan the only writer of daniel-server's since H6.3d. So neither install task may
mention `k3s_version` in its `when:`: a pin bump would make Ansible reinstall the binary SUC is
replacing, and the two writers would fight over it. Each task still runs for a missing binary
(a fresh host). The agent's also re-runs the installer with `INSTALL_K3S_SKIP_DOWNLOAD` when
only an argument is missing, which rewrites the unit and never the binary. The server's runs
for a missing binary alone: its flags are daniel-box's package's since H6.4b.

Each test here encodes a way the guard regresses while everything still reads green:

  * The version clause returns, or the agent's skip-download flag goes. Either makes a second
    writer of the version.

  * The agent's args clause goes. A changed agent flag would then never reach the unit.

  * The server's args clause returns. The installer would then rewrite the unit's flags
    behind the package's back, a second writer of the server's flags.

  * The binary read loses `check_mode: false`. A `command` task is skipped under `--check`,
    which leaves its register undefined — and it is the guard consuming it two tasks later
    that fails, not the read. That is the same shape as the repo's
    check-mode-breaks-downstream-consumers class, and grepping for `retries:` would miss it.

  * The agent stops reading its own binary. The agent is a separate host, so the server's
    reading says nothing about it.

Run: uv run pytest ansible/tests/setup/test_k3s_version_guard.py
"""

import pytest

from _helpers import load_tasks
from lib.repo_paths import K3S_ROLE


K3S_TASKS = K3S_ROLE / "tasks"

# (task file, install-task name, the register its binary read must fill)
INSTALLS = [
    ("server.yml", "Install the k3s server", "k3s_installed_version"),
    (
        "agent.yml",
        "Install or reconfigure the k3s agent",
        "k3s_agent_installed_version",
    ),
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


def test_the_agent_install_condition_still_consults_its_args() -> None:
    """Dropping the version clause must not drop the agent's args check (until H6.5b).

    A condition that watched only the binary would stop reacting to a changed flag, which
    is the regression the args guard exists to prevent.
    """
    _, name, _ = INSTALLS[1]
    assert "k3s_agent_args" in str(_named("agent.yml", name).get("when", ""))


def test_the_server_install_passes_no_flags() -> None:
    """The package owns the server's flags, so the installer gets a bare `server`."""
    _, name, register = INSTALLS[0]
    task = _named("server.yml", name)
    assert task["environment"]["INSTALL_K3S_EXEC"] == "server"
    assert str(task["when"]) == f"({register}.rc | default(1)) != 0"


@pytest.mark.parametrize("filename,name,register", INSTALLS)
def test_install_leaves_the_version_to_suc(filename, name, register) -> None:
    """The version clause is gone, and a missing binary still runs the install."""
    when = str(_named(filename, name).get("when", ""))
    assert "k3s_version" not in when, (
        f"{filename}: SUC owns the version; the guard must not"
    )
    assert register in when, f"{filename}: a missing binary must still run the install"


def test_an_agent_args_only_rerun_never_downloads_a_binary() -> None:
    _, name, register = INSTALLS[1]
    env = _named("agent.yml", name).get("environment", {})
    assert f"{register}.rc" in str(env["INSTALL_K3S_SKIP_DOWNLOAD"])


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


def test_each_host_reads_its_own_version() -> None:
    """The agent must not share the server's register.

    They are different hosts. One register across both would let the agent's guard read the
    server's binary, so a missing agent binary would never be installed.
    """
    registers = set()
    for filename, name, expected_register in INSTALLS:
        reads = _version_reads(filename)
        found = {t.get("register") for t in reads}
        assert expected_register in found, (
            f"{filename}: expected the version read to register "
            f"{expected_register!r}, found {found!r}"
        )
        assert expected_register in str(_named(filename, name).get("when", "")), (
            f"{filename}: the install guard does not consult {expected_register!r}"
        )
        registers |= found
    assert len(registers) == len(INSTALLS), (
        f"server and agent must use distinct registers, got {registers!r}"
    )
