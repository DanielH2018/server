#!/usr/bin/env python3
"""A Claude agent's own Unix user reaches nothing of the operator's that it was not handed.

`roles/setup/common/tasks/agent_user.yml` builds the user for renovate-agent and for the
operator's own sessions (`claude_code_agent_user`). The user exists so a session cannot read
the age key, which decrypts the become password, nor the operator's gh token and ssh keys. Each
test pins one leg of that. A leg that slips fails no deploy: the user still works, only with
more reach than the design gives it.

Run: uv run pytest ansible/tests/setup/test_agent_user.py
"""

import re

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from renderer.renderer_lib import host as host_mode

SETUP = ANSIBLE / "roles" / "setup"
SHARED = SETUP / "common" / "tasks" / "agent_user.yml"
SHARED_IMPORT = "{{ role_path }}/../common/tasks/agent_user.yml"
# The file in each role that imports the shared one.
AGENT_TASKS_FILE = {"renovate_agent": "main.yml"}
# Every role that builds an agent user, with the role variable each contract key must name.
AGENTS = {
    "renovate_agent": {
        "agent_user_name": "{{ renovate_agent_user }}",
        "agent_user_home": "{{ renovate_agent_home }}",
        "agent_user_clone_dir": "{{ renovate_agent_clone_dir }}",
        "agent_user_repo": "{{ renovate_agent_repo }}",
        "agent_user_uv_version": "{{ initial_setup_uv_version }}",
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
    role_tasks = tasks(SETUP / role / "tasks" / AGENT_TASKS_FILE[role])
    imports = [
        t for t in role_tasks if t.get("ansible.builtin.import_tasks") == SHARED_IMPORT
    ]
    assert len(imports) == 1, f"{role} must import {SHARED.name} exactly once"
    assert imports[0]["vars"] == AGENTS[role]


def test_the_agent_user_joins_no_group() -> None:
    """No group: `ubuntu` reads /etc/rancher/k3s/*.env, and `sudo` is root."""
    assert group_grants(tasks(SHARED)) == []


def test_the_packaged_claude_agent_joins_only_the_holder_journal_ssh_and_socket_groups() -> (
    None
):
    """`usermod --groups` sets the whole list, so an added group has to be written into this one."""
    setup = host_mode.render_host("daniel-box").files[
        "/usr/lib/homelab/claude-agent-setup"
    ][0]
    found = re.findall(rb"^groups=\((.*)\)$", setup, re.M)
    assert len(found) == 1
    assert set(found[0].split()) <= {
        b"worktree-holders",
        b"homelab-rootq",
        b"systemd-journal",
        b"ssh-users",
    }
    assert b"sudo" not in found[0]


def test_a_user_task_granting_a_group_is_flagged() -> None:
    granting = [
        {"name": "Create it", "ansible.builtin.user": {"name": "x", "groups": "sudo"}}
    ]
    assert group_grants(granting) == ["Create it: groups"]


def test_claude_code_installs_into_the_agents_home_under_become() -> None:
    """The installer refuses to run under sudo unless told the home is deliberate."""
    task = named(tasks(SHARED), "Install Claude Code for the agent's user")
    assert task["environment"] == {
        "HOME": "{{ agent_user_home }}",
        "TMPDIR": "/tmp",
        "CLAUDE_INSTALL_ALLOW_SUDO": "1",
    }


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


def reowns_only_roots_files(argv: list[str]) -> bool:
    """Whether a command is `find <dir> -user root ... -exec chown`, the closing handover."""
    return (
        argv[:1] == ["find"]
        and any(argv[i : i + 2] == ["-user", "root"] for i in range(len(argv)))
        and "chown" in argv
    )


def chown_owner_group(task_list: list[dict]) -> tuple[str, str]:
    """The `owner:group` the closing handover chowns root's files to."""
    argv = named(task_list, "Hand the agent's home to its own user")[
        "ansible.builtin.command"
    ]["argv"]
    owner, group = argv[argv.index("-h") + 1].split(":")
    return owner, group


def root_run_writers(task_list: list[dict]) -> list[str]:
    """Every command task that runs as root on each apply rather than as the agent (#3510).

    A command run as root inside the agent's home leaves root-owned files there, and the
    closing chown then reports `changed` on every apply. A `creates:`-gated command runs once,
    and the chown hands its output over on that first apply. The chown itself is exempt: it
    writes only to files root owns, so on a converged home it writes nothing.
    """
    flagged = []
    for t in task_list:
        cmd = t.get("ansible.builtin.command")
        if cmd is None or "creates" in cmd or "creates" in (t.get("args") or {}):
            continue
        if reowns_only_roots_files(cmd.get("argv") or []):
            continue
        if (cmd.get("argv") or [None])[0] != "runuser":
            flagged.append(str(t.get("name")))
    return flagged


def test_every_command_writing_the_agents_home_on_each_apply_runs_as_the_agent() -> (
    None
):
    shared = tasks(SHARED)
    # The named member, so the census cannot pass on a renamed or vanished task.
    named(shared, "Install the pinned host Python for the agent user")
    assert root_run_writers(shared) == []


def test_a_command_run_as_root_on_each_apply_is_flagged() -> None:
    root_run = {
        "name": "Install it",
        "ansible.builtin.command": {"cmd": "/srv/agent/.local/bin/uv python install"},
    }
    run_once = {
        "name": "Clone it",
        "ansible.builtin.command": {"argv": ["git", "clone"], "creates": "/srv/x"},
    }
    assert root_run_writers([root_run, run_once]) == ["Install it"]


def test_the_handover_reowns_only_roots_files() -> None:
    """#3607: a recursive chown of the whole home re-grouped the agent's own run-time files."""
    task = named(tasks(SHARED), "Hand the agent's home to its own user")
    assert reowns_only_roots_files(task["ansible.builtin.command"]["argv"])
    assert task["changed_when"] == "common_agent_user_home_handover.stdout | length > 0"


def test_a_chown_of_every_file_is_not_the_handover() -> None:
    everything = ["find", "/srv/agent", "-exec", "chown", "agent:ops", "{}", "+"]
    assert not reowns_only_roots_files(everything)
    assert root_run_writers(
        [{"name": "Chown all", "ansible.builtin.command": {"argv": everything}}]
    ) == ["Chown all"]


def dirs_not_owned_like_the_chown(task_list: list[dict]) -> list[str]:
    """Each directory task whose owner or group differs from the closing chown's.

    The commands that run as the agent write into these directories, so a root-owned one
    fails them on a fresh host's first apply, before the closing chown has run.
    """
    owner_group = chown_owner_group(task_list)
    return [
        str(t.get("name"))
        for t in task_list
        if (f := t.get("ansible.builtin.file") or {}).get("state") == "directory"
        and (f.get("owner"), f.get("group")) != owner_group
    ]


def test_every_directory_the_agent_writes_into_is_the_agents_from_its_first_apply() -> (
    None
):
    shared = tasks(SHARED)
    named(
        shared,
        "Create the agent user's bin, claude-guard and claude-worktree directories",
    )
    assert dirs_not_owned_like_the_chown(shared) == []


def test_a_root_owned_directory_is_flagged() -> None:
    chown = named(tasks(SHARED), "Hand the agent's home to its own user")
    root_dir = {
        "name": "Create bin",
        "ansible.builtin.file": {"path": "/srv/agent/.local/bin", "state": "directory"},
    }
    assert dirs_not_owned_like_the_chown([root_dir, chown]) == ["Create bin"]


WORKTREE_BOOTSTRAP = ANSIBLE.parent / "scripts" / "lib" / "_claude_worktree.py"
WORKTREE_COPY = "Give the agent user the operator's claude_worktree module"


def worktree_lookup_dir() -> str:
    """The home-relative directory `_claude_worktree.py` imports the module from."""
    found = re.search(
        r'"CLAUDE_WORKTREE_HOME", "~(/[^"]+)"', WORKTREE_BOOTSTRAP.read_text()
    )
    assert found, "the bootstrap no longer names its claude_worktree directory"
    return found.group(1)


def worktree_copy_problems(task_list: list[dict], rel: str) -> list[str]:
    """Where the claude_worktree copy misses the directory the bootstrap reads."""
    copy = named(task_list, WORKTREE_COPY)["ansible.builtin.copy"]
    want = "{{ agent_user_home }}" + rel + "/claude_worktree.py"
    problems = [] if copy["dest"] == want else [f"dest {copy['dest']}"]
    if copy["src"] != "/home/{{ sys_user }}" + rel + "/claude_worktree.py":
        problems.append(f"src {copy['src']}")
    return problems


def test_the_agent_gets_claude_worktree_where_the_bootstrap_looks_for_it() -> None:
    """Without the module lib.worktrees raises, and findings.py fails for the agent (#4033)."""
    rel = worktree_lookup_dir()
    assert rel == "/.local/share/claude-worktree"
    shared = tasks(SHARED)
    assert worktree_copy_problems(shared, rel) == []
    dirs = named(
        shared,
        "Create the agent user's bin, claude-guard and claude-worktree directories",
    )
    assert "{{ agent_user_home }}" + rel in dirs["loop"]


def test_a_claude_worktree_copy_to_another_directory_is_flagged() -> None:
    task = {
        "name": WORKTREE_COPY,
        "ansible.builtin.copy": {
            "src": "/home/{{ sys_user }}/.local/share/claude-worktree/claude_worktree.py",
            "dest": "{{ agent_user_home }}/.local/share/claude-guard/claude_worktree.py",
        },
    }
    assert worktree_copy_problems([task], "/.local/share/claude-worktree") == [
        "dest {{ agent_user_home }}/.local/share/claude-guard/claude_worktree.py"
    ]
