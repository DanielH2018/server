#!/usr/bin/env python3
"""Guards the k3s install tasks against reverting to an args-only condition.

Both install tasks pass `INSTALL_K3S_VERSION` to the installer, so it is easy to read them
as version-aware. They are not, unless their `when:` says so. Until 2026-08-22 the condition
diffed `k3s_server_args` / `k3s_agent_args` against the installed systemd unit and nothing
else, which means a version-only bump left every argument identical, the guard read false,
and the installer never ran. The pin moved, the node did not, and the play reported `ok` —
`k3s_version` occurred four times in the whole tree and none of them was a comparison.

Renovate opens that exact change (PR #199, v1.36.2+k3s1 -> v1.36.3+k3s1), so the silent
no-op is on the routine upgrade path, not a corner case.

Each test here encodes a way the fix regresses while everything still reads green:

  * The `when:` stops mentioning `k3s_version`. That is the whole defect returning; nothing
    else in the role would notice, because passing the version to the installer that never
    runs looks exactly like passing it to one that does.

  * The version read loses `check_mode: false`. A `command` task is skipped under `--check`,
    which leaves its register undefined — and it is the guard consuming it two tasks later
    that fails, not the read. That is the same shape as the repo's
    check-mode-breaks-downstream-consumers class, and grepping for `retries:` would miss it.

  * The agent stops reading its own version. The agent is a separate host, so the server's
    reading says nothing about it; sharing one register across the two would leave the agent
    a version behind its server, which k3s does not support.

Run: uv run pytest ansible/tests/test_k3s_version_guard.py
"""

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
K3S_TASKS = ANSIBLE / "roles" / "setup" / "k3s" / "tasks"

# (task file, install-task name, the register its version read must fill)
INSTALLS = [
    ("server.yml", "Install or reconfigure the k3s server", "k3s_installed_version"),
    (
        "agent.yml",
        "Install or reconfigure the k3s agent",
        "k3s_agent_installed_version",
    ),
]


def _tasks(filename: str) -> list[dict]:
    return yaml.safe_load((K3S_TASKS / filename).read_text())


def _named(filename: str, name: str) -> dict:
    for task in _tasks(filename):
        if task.get("name") == name:
            return task
    raise AssertionError(f"{filename} has no task named {name!r}")


def _version_reads(filename: str) -> list[dict]:
    """Tasks that run `k3s --version`, whatever they are called."""
    return [
        t
        for t in _tasks(filename)
        if "--version" in str(t.get("ansible.builtin.command", {}).get("cmd", ""))
    ]


def test_install_condition_consults_the_version() -> None:
    """A version-only bump must reach the installer.

    Without this the guard is args-only: `k3s_version` changes, every argument stays the
    same, the condition is false and k3s is never upgraded — silently, with the play green.
    """
    for filename, name, _ in INSTALLS:
        when = str(_named(filename, name).get("when", ""))
        assert "k3s_version" in when, (
            f"{filename}: the install task's `when:` no longer mentions k3s_version, so a "
            f"version bump cannot trigger it. Passing INSTALL_K3S_VERSION to an installer "
            f"that never runs is the defect this guards — see this module's docstring."
        )


def test_install_condition_still_consults_the_args() -> None:
    """The control: adding the version check must not drop the original args check.

    A condition that watched only the version would stop reacting to a changed flag, which
    is the regression the args guard was itself introduced to fix.
    """
    for filename, name, _ in INSTALLS:
        when = str(_named(filename, name).get("when", ""))
        expected = "k3s_server_args" if filename == "server.yml" else "k3s_agent_args"
        assert expected in when, (
            f"{filename}: the install task's `when:` no longer mentions {expected}. The "
            f"version check is an addition to the args check, never a replacement."
        )


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

    They are different hosts. One register across both would let the agent sit a version
    behind the server, which k3s does not support, while the guard reads satisfied.
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
