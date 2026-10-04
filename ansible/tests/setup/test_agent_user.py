#!/usr/bin/env python3
"""A Claude agent's own Unix user reaches nothing of the operator's that it was not handed.

`roles/setup/common/tasks/agent_user.yml` builds the user for renovate-agent and for the
operator's own sessions (`claude_code_agent_user`). The user exists so a session cannot read
the age key, which decrypts the become password, nor the operator's gh token and ssh keys. Each
test pins one leg of that. A leg that slips fails no deploy: the user still works, only with
more reach than the design gives it.

Run: uv run pytest ansible/tests/setup/test_agent_user.py
"""

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

SETUP = ANSIBLE / "roles" / "setup"
SHARED = SETUP / "common" / "tasks" / "agent_user.yml"
SHARED_IMPORT = "{{ role_path }}/../common/tasks/agent_user.yml"
CLAUDE_TASKS = SETUP / "claude_code" / "tasks" / "main.yml"
# Every role that builds an agent user, with the role variable each contract key must name.
AGENTS = {
    "renovate_agent": {
        "agent_user_name": "{{ renovate_agent_user }}",
        "agent_user_home": "{{ renovate_agent_home }}",
        "agent_user_clone_dir": "{{ renovate_agent_clone_dir }}",
        "agent_user_repo": "{{ renovate_agent_repo }}",
    },
    "claude_code": {
        "agent_user_name": "{{ claude_code_agent_user }}",
        "agent_user_home": "{{ claude_code_agent_user_home }}",
        "agent_user_clone_dir": "{{ claude_code_agent_user_clone_dir }}",
        "agent_user_repo": "{{ claude_code_agent_user_repo }}",
    },
}
# The user module's keys that add a user to a group beyond its own.
GROUP_KEYS = ("groups", "group", "append")


def tasks(path) -> list[dict]:
    return yaml_fast.safe_load(path.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def group_grants(task_list: list[dict]) -> list[str]:
    """Every user-module task that hands its user a group, as `<task name>: <key>`."""
    return [
        f"{t.get('name')}: {key}"
        for t in task_list
        for key in GROUP_KEYS
        if key in (t.get("ansible.builtin.user") or {})
    ]


@pytest.mark.parametrize("role", sorted(AGENTS))
def test_each_agent_user_is_built_by_the_shared_file_with_its_own_paths(
    role: str,
) -> None:
    role_tasks = tasks(SETUP / role / "tasks" / "main.yml")
    imports = [
        t for t in role_tasks if t.get("ansible.builtin.import_tasks") == SHARED_IMPORT
    ]
    assert len(imports) == 1, f"{role} must import {SHARED.name} exactly once"
    assert imports[0]["vars"] == AGENTS[role]


def test_the_agent_user_joins_no_group() -> None:
    """No group: `ubuntu` reads /etc/rancher/k3s/*.env, and `sudo` is root."""
    assert group_grants(tasks(SHARED)) == []
    assert group_grants(tasks(CLAUDE_TASKS)) == []


def test_a_user_task_granting_a_group_is_flagged() -> None:
    granting = [
        {"name": "Create it", "ansible.builtin.user": {"name": "x", "groups": "sudo"}}
    ]
    assert group_grants(granting) == ["Create it: groups"]


def test_the_agent_gets_the_read_only_kubeconfig_and_never_prints_it() -> None:
    task = named(
        tasks(CLAUDE_TASKS), "Give the agent user the operator's read-only kubeconfig"
    )
    copy = task["ansible.builtin.copy"]
    assert copy["src"] == "/home/{{ sys_user }}/.kube/config", (
        "the operator's kubeconfig is the homelab-readonly ServiceAccount; "
        "/etc/rancher/k3s/k3s.yaml is cluster-admin"
    )
    assert copy["mode"] == "0600"
    assert task["diff"] is False and task["no_log"] is True


def test_the_login_profile_points_the_hook_shim_at_the_agents_own_clone_and_uv() -> (
    None
):
    """The shim defaults to the operator's checkout, which this user cannot cd into."""
    profile = render_setup_text(
        "claude_code",
        "agent-user-profile.j2",
        {
            "claude_code_agent_user_home": "/srv/agent",
            "claude_code_agent_user_clone_dir": "/srv/clone",
        },
    )
    assert "\nexport RUN_HOOK_PROJECT_DIR=/srv/clone\n" in profile
    assert "\nexport RUN_HOOK_UV=/srv/agent/.local/bin/uv\n" in profile
    assert '\nPATH="$HOME/.local/bin:$PATH"\n' in profile
    shim = (ANSIBLE.parent / ".claude" / "hooks" / "run-hook.sh").read_text()
    assert "${RUN_HOOK_PROJECT_DIR:-" in shim and "${RUN_HOOK_UV:-" in shim, (
        "the hook shim no longer reads the overrides this profile sets"
    )


def test_switching_the_agent_user_off_expires_it_and_on_lifts_the_expiry() -> None:
    """Expired, never removed: the home holds the agent's clone and unpushed work."""
    claude_tasks = tasks(CLAUDE_TASKS)
    off = named(claude_tasks, "Expire the agent user's account when it is switched off")
    on = named(claude_tasks, "Lift the agent user's account expiry")
    # 0 would be day 0 in /etc/shadow, which also reads as "never expires".
    assert off["ansible.builtin.user"]["expires"] >= 86400
    assert "not claude_code_agent_user_enabled" in off["when"]
    assert on["ansible.builtin.user"]["expires"] == -1
    assert on["when"] == "claude_code_agent_user_enabled"
    removals = [
        t.get("name")
        for t in claude_tasks
        if (t.get("ansible.builtin.user") or {}).get("state") == "absent"
    ]
    assert removals == []
