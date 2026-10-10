#!/usr/bin/env python3
"""The claude_code role fast-forwards the agent's clone only when it is a clean master.

The hook shim and the homelab-ui launcher run from that clone's main working tree, which the
shared agent_user.yml clones once and never pulls (#4067, #4099). The gates are evaluated with
Ansible's own Templar against `git status --porcelain=v2 --branch` output, so a test passes
only if the gates decide correctly, not only if their text is present.

Run: uv run pytest ansible/tests/setup/test_agent_clone_fast_forward.py
"""

import pytest
from _helpers import ANSIBLE
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast

CLAUDE_TASKS = ANSIBLE / "roles" / "setup" / "claude_code" / "tasks" / "main.yml"
READ = "Read the agent user's clone branch and status"
PULL = "Fast-forward the agent user's clone to origin's master"
REPORT = "Report that the agent user's clone was left behind"
SYNC = "Sync the repo's venv in the agent user's clone"

HEAD = "# branch.oid 0123abc\n"
UPSTREAM = "# branch.upstream origin/master\n# branch.ab +0 -94\n"
CHANGED = "1 .M N... 100644 100644 100644 aaa bbb scripts/diagnostics/ui_mcp.sh\n"
# `git status --porcelain=v2 --branch --untracked-files=no` output, and whether to pull.
STATUSES = {
    "clean master": (HEAD + "# branch.head master\n" + UPSTREAM, True),
    "master with a modified file": (
        HEAD + "# branch.head master\n" + UPSTREAM + CHANGED,
        False,
    ),
    "detached HEAD": (HEAD + "# branch.head (detached)\n", False),
    "another branch": (HEAD + "# branch.head worktree-foo\n", False),
    "a branch named after master": (HEAD + "# branch.head master-old\n", False),
}


def tasks() -> list[dict]:
    return yaml_fast.safe_load(CLAUDE_TASKS.read_text())


def named(name: str) -> dict:
    found = [t for t in tasks() if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def holds(conditions: list[str], variables: dict) -> bool:
    """Whether every `when:` condition is true, evaluated by Ansible's Templar."""
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.template(trust_as_template("{{ " + c + " }}")) for c in conditions
    )


def pulls(status: dict) -> bool:
    return holds(
        named(PULL)["when"],
        {
            "claude_code_agent_user_enabled": True,
            "claude_code_agent_clone_status": status,
        },
    )


@pytest.mark.parametrize("case", sorted(STATUSES))
def test_the_pull_runs_only_on_a_clean_master(case: str) -> None:
    stdout, expected = STATUSES[case]
    assert pulls({"rc": 0, "stdout": stdout}) is expected


def test_a_failed_or_skipped_status_read_skips_the_pull() -> None:
    clean = STATUSES["clean master"][0]
    assert not pulls(
        {"rc": 128, "stdout": clean, "stderr": "fatal: not a git repository"}
    )
    assert not pulls({"skipped": True})


def test_the_pull_runs_as_the_agent_and_never_fails_the_apply() -> None:
    """A red task holds the GitOps deployer over the agent's own working state."""
    for name in (READ, PULL):
        task = named(name)
        assert task["ansible.builtin.command"]["argv"][:4] == [
            "runuser",
            "-u",
            "{{ claude_code_agent_user }}",
            "--",
        ]
        assert task["failed_when"] is False, f"{name!r} must not fail the apply"
    assert named(PULL)["ansible.builtin.command"]["argv"][4:] == [
        "git",
        "-C",
        "{{ claude_code_agent_user_clone_dir }}",
        "pull",
        "--ff-only",
        "origin",
        "master",
    ]


def test_the_report_fires_when_the_clone_was_left_behind_and_not_after_a_pull() -> None:
    conditions = named(REPORT)["when"]
    base = {"claude_code_agent_user_enabled": True, "ansible_check_mode": False}
    assert holds(conditions, base | {"claude_code_agent_clone_pull": {"skipped": True}})
    assert holds(conditions, base | {"claude_code_agent_clone_pull": {"rc": 1}})
    assert not holds(conditions, base | {"claude_code_agent_clone_pull": {"rc": 0}})


def test_the_venv_sync_reads_the_fast_forwarded_lock() -> None:
    names = [t.get("name") for t in tasks()]
    assert names.index(READ) < names.index(PULL) < names.index(SYNC)
