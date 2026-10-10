#!/usr/bin/env python3
"""The claude agent user reads the GitOps deployer's state directory, and only reads it (#4198).

CLAUDE.md's *When to wait* has every session check `hold_sha` and `owed.jsonl` before it lands.
The directory is 0750 sys_user and the agent belongs to no group, so the agent can read it
only through the ACL `claude_code/tasks/agent_access.yml` grants.

Run: uv run pytest ansible/tests/setup/test_agent_gitops_read.py
"""

from _helpers import ANSIBLE
from lib import yaml_fast

AGENT_ACCESS = ANSIBLE / "roles/setup/claude_code/tasks/agent_access.yml"
DEFAULTS = ANSIBLE / "roles/setup/claude_code/defaults/main.yml"
GRANT = "Let the agent user read the deployer's hold markers and owed ledger"


def grant_problems(task: dict) -> list[str]:
    """Where an ACL task falls short of a read-only, switchable grant on the state directory."""
    acl = task.get("ansible.posix.acl") or {}
    problems = []
    if acl.get("path") != "/var/lib/gitops-deploy":
        problems.append(f"path {acl.get('path')}")
    if acl.get("entity") != "{{ claude_code_agent_user }}":
        problems.append(f"entity {acl.get('entity')}")
    if acl.get("recursive"):
        problems.append("recursive")
    if "claude_code_agent_gitops_read" not in str(acl.get("state")):
        problems.append("no switch decides its state")
    loop = task.get("loop") or []
    if any("w" in str(item.get("permissions")) for item in loop):
        problems.append("grants write")
    if {"permissions": "r", "default": True} not in loop:
        problems.append(
            "no default entry, so a marker the deployer rewrites is unreadable"
        )
    if "has_gitops" not in str(task.get("when")):
        problems.append("not gated on has_gitops, so setfacl fails on a non-deployer")
    return problems


def test_the_agent_gets_a_read_only_grant_on_the_deployers_state() -> None:
    access = yaml_fast.safe_load(AGENT_ACCESS.read_text())
    found = [t for t in access if t.get("name") == GRANT]
    assert len(found) == 1, f"expected one task named {GRANT!r}"
    assert grant_problems(found[0]) == []
    assert (
        yaml_fast.safe_load(DEFAULTS.read_text())["claude_code_agent_gitops_read"]
        is True
    )


def test_a_writable_ungated_grant_without_a_default_entry_is_flagged() -> None:
    task = {
        "ansible.posix.acl": {
            "path": "/var/lib/gitops-deploy",
            "entity": "{{ claude_code_agent_user }}",
            "state": "present",
        },
        "loop": [{"permissions": "rwX", "default": False}],
    }
    assert grant_problems(task) == [
        "no switch decides its state",
        "grants write",
        "no default entry, so a marker the deployer rewrites is unreadable",
        "not gated on has_gitops, so setfacl fails on a non-deployer",
    ]
