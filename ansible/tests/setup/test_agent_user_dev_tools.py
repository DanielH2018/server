#!/usr/bin/env python3
"""The claude agent user gets the commit hooks and the Ansible collections the operator has.

Without prek the agent's commits skipped gitleaks and the commit-time ratchets (#4100). Without
a readable collections fallback every playbook it ran from a worktree died at `couldn't resolve
module/action` (#4108). Each test pins one leg of the fix, with a reject case beside it.

Run: uv run pytest ansible/tests/setup/test_agent_user_dev_tools.py
"""

from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast

SETUP = ANSIBLE / "roles" / "setup"
SHARED = SETUP / "common" / "tasks" / "agent_user.yml"
CLAUDE_TASKS = SETUP / "claude_code" / "tasks" / "main.yml"
PREK_COPY = "Give the agent user the operator's pinned prek"
HOOK_INSTALL = "Install the commit hooks in the agent user's clone"
GALAXY = "Install the pinned Ansible collections in the agent user's clone"


def tasks(path) -> list[dict]:
    return yaml_fast.safe_load(path.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def hook_install_problems(task: dict) -> list[str]:
    """Where the hook install misses running the agent's own prek, once, as the agent."""
    cmd = task["ansible.builtin.command"]
    argv = cmd.get("argv") or []
    problems = []
    if argv[:4] != ["runuser", "-u", "{{ claude_code_agent_user }}", "--"]:
        problems.append("not run as the agent")
    if argv[4:] != ["{{ claude_code_agent_user_home }}/.local/bin/prek", "install"]:
        problems.append(f"argv {argv[4:]}")
    if cmd.get("chdir") != "{{ claude_code_agent_user_clone_dir }}":
        problems.append(f"chdir {cmd.get('chdir')}")
    if (
        cmd.get("creates")
        != "{{ claude_code_agent_user_clone_dir }}/.git/hooks/pre-commit"
    ):
        problems.append(f"creates {cmd.get('creates')}")
    return problems


def test_the_agent_gets_the_operators_prek_binary_not_its_symlink() -> None:
    """~/.local/bin/prek is a symlink into the operator's uv tools; a copy of it dangles."""
    shared = tasks(SHARED)
    copy = named(shared, PREK_COPY)["ansible.builtin.copy"]
    assert copy["src"] == "/home/{{ sys_user }}/.local/share/uv/tools/prek/bin/prek"
    assert copy["dest"] == "{{ agent_user_home }}/.local/bin/prek"
    uv = named(shared, "Give the agent user the operator's pinned uv and uvx")
    assert uv["loop"] == ["uv", "uvx"]


def test_the_agents_clone_gets_hooks_from_its_own_prek() -> None:
    task = named(tasks(CLAUDE_TASKS), HOOK_INSTALL)
    assert hook_install_problems(task) == []
    # A host whose operator has no prek copies none, and the install must skip there.
    assert task["ansible.builtin.command"]["removes"] == (
        "{{ claude_code_agent_user_home }}/.local/bin/prek"
    )
    # renovate_agent imports the shared file; only claude_code's agent gets the hooks.
    shared_argvs = [
        (t.get("ansible.builtin.command") or {}).get("argv") or []
        for t in tasks(SHARED)
    ]
    assert not [a for a in shared_argvs if a[-2:-1] and a[-2].endswith("/prek")]


def test_a_hook_install_run_as_root_or_with_the_operators_prek_is_flagged() -> None:
    task = {
        "ansible.builtin.command": {
            "argv": ["/home/ubuntu/.local/bin/prek", "install"],
            "chdir": "{{ claude_code_agent_user_clone_dir }}",
            "creates": "{{ claude_code_agent_user_clone_dir }}/.git/hooks/pre-commit",
        }
    }
    assert hook_install_problems(task) == [
        "not run as the agent",
        "argv []",
    ]


def collections_path_problems(profile: str, clone: str) -> list[str]:
    """Where the profile's collections path misses the local-first, clone-fallback order."""
    lines = [
        ln
        for ln in profile.splitlines()
        if ln.startswith("export ANSIBLE_COLLECTIONS_PATH=")
    ]
    if len(lines) != 1:
        return [f"{len(lines)} ANSIBLE_COLLECTIONS_PATH exports"]
    entries = lines[0].split("=", 1)[1].split(":")
    want = ["ansible/collections", f"{clone}/ansible/collections"]
    return [] if entries == want else [f"entries {entries}"]


def test_the_profile_falls_back_to_the_agents_own_collections() -> None:
    clone = "/srv/clone"
    profile = render_setup_text(
        "claude_code",
        "agent-user-profile.j2",
        {
            "claude_code_agent_user_home": "/srv/agent",
            "claude_code_agent_user_clone_dir": clone,
        },
    )
    assert collections_path_problems(profile, clone) == []
    # The fallback is only worth something where the role installs into it.
    argv = named(tasks(CLAUDE_TASKS), GALAXY)["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["runuser", "-u", "{{ claude_code_agent_user }}", "--"]
    assert argv[-4:] == ["-r", "ansible/requirements.yml", "-p", "ansible/collections"]


def test_an_operator_fallback_or_a_clone_first_path_is_flagged() -> None:
    clone = "/srv/clone"
    operator = "export ANSIBLE_COLLECTIONS_PATH=ansible/collections:/home/ubuntu/server/ansible/collections\n"
    clone_first = f"export ANSIBLE_COLLECTIONS_PATH={clone}/ansible/collections:ansible/collections\n"
    assert collections_path_problems(operator, clone) != []
    assert collections_path_problems(clone_first, clone) != []
    assert collections_path_problems("PATH=x\n", clone) == [
        "0 ANSIBLE_COLLECTIONS_PATH exports"
    ]
