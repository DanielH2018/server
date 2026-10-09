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
# Imported by main.yml under one `when:`, so its tasks carry none and agent_tasks() misses them.
AGENT_GITHUB = SETUP / "claude_code" / "tasks" / "agent_github.yml"
AGENT_ACCESS = SETUP / "claude_code" / "tasks" / "agent_access.yml"
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
    assert group_grants(tasks(AGENT_GITHUB)) == []


def test_the_claude_agents_only_group_is_the_journal_and_it_switches_off() -> None:
    """The operator granted systemd-journal on 2026-10-09, and nothing else.

    `append: false` makes the list exact, so the switch's false arm removes the group and an
    added group would have to be written into this one list.
    """
    access = tasks(AGENT_ACCESS)
    assert group_grants(access) == [
        "Set the agent user's journal access: groups",
        "Set the agent user's journal access: append",
    ]
    user = named(access, "Set the agent user's journal access")["ansible.builtin.user"]
    assert user["groups"] == (
        "{{ ['systemd-journal'] if claude_code_agent_journal_access else [] }}"
    )
    assert user["append"] is False


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
        "CLAUDE_INSTALL_ALLOW_SUDO": "1",
    }


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


def reowns_only_roots_files(argv: list[str]) -> bool:
    """Whether a command is `find <dir> -user root ... -exec chown`, the closing handover."""
    return argv[:1] == ["find"] and argv[2:4] == ["-user", "root"] and "chown" in argv


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


def agent_tasks(task_list: list[dict]) -> list[dict]:
    """claude_code's tasks that act on the agent user's home."""
    return [t for t in task_list if t.get("when") == "claude_code_agent_user_enabled"]


def test_every_command_writing_the_agents_home_on_each_apply_runs_as_the_agent() -> (
    None
):
    shared = tasks(SHARED)
    claude = agent_tasks(tasks(CLAUDE_TASKS))
    github = tasks(AGENT_GITHUB)
    # The named members, so the census cannot pass on a renamed or vanished task.
    named(shared, "Install the pinned host Python for the agent user")
    named(claude, "Sync the repo's venv in the agent user's clone")
    named(github, "Generate the agent's commit-signing key")
    assert root_run_writers(shared) == []
    assert root_run_writers(claude) == []
    assert root_run_writers(github) == []


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


def test_the_agents_clone_gets_a_venv_its_hooks_can_import_from() -> None:
    """#3513: the hook shim runs `uv run --no-sync` in the clone the profile names."""
    task = named(tasks(CLAUDE_TASKS), "Sync the repo's venv in the agent user's clone")
    cmd = task["ansible.builtin.command"]
    assert cmd["argv"][-2:] == ["sync", "--frozen"]
    assert cmd["chdir"] == "{{ claude_code_agent_user_clone_dir }}"
    assert task["environment"] == {"HOME": "{{ claude_code_agent_user_home }}"}


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
    github = tasks(AGENT_GITHUB)
    named(shared, "Create the agent user's bin and claude-guard directories")
    named(github, "Create the agent user's ssh and git config directories")
    assert dirs_not_owned_like_the_chown(shared) == []
    # claude_code names the agent by its own variable, which its import maps to agent_user_name.
    owners = {
        (f.get("owner"), f.get("group"))
        for t in github
        if (f := t.get("ansible.builtin.file") or {}).get("state") == "directory"
    }
    assert owners == {("{{ claude_code_agent_user }}", "{{ sys_user }}")}


def test_a_root_owned_directory_is_flagged() -> None:
    chown = named(tasks(SHARED), "Hand the agent's home to its own user")
    root_dir = {
        "name": "Create bin",
        "ansible.builtin.file": {"path": "/srv/agent/.local/bin", "state": "directory"},
    }
    assert dirs_not_owned_like_the_chown([root_dir, chown]) == ["Create bin"]


def chmods_after_the_acl(task_list: list[dict]) -> list[str]:
    """Each file task that sets a mode after the first ACL task, which would reset its mask."""
    acl_at = next(
        (i for i, t in enumerate(task_list) if "ansible.posix.acl" in t), len(task_list)
    )
    return [
        t.get("name", "")
        for t in task_list[acl_at:]
        if "mode" in (t.get("ansible.builtin.file") or {})
    ]


def test_the_operator_read_acl_runs_after_every_mode_it_depends_on() -> None:
    """A later chmod rewrites the ACL mask, and a mask of --- disables the operator's entry."""
    access = tasks(AGENT_ACCESS)
    named(access, "Give files the agent writes there the operator's read")
    assert chmods_after_the_acl(access) == []
    main = [t.get("ansible.builtin.import_tasks") for t in tasks(CLAUDE_TASKS)]
    assert main.index("agent_github.yml") < main.index("agent_access.yml")


def test_a_chmod_after_the_acl_is_flagged() -> None:
    acl = {"name": "Grant", "ansible.posix.acl": {"path": "/x"}}
    chmod = {"name": "Reset", "ansible.builtin.file": {"path": "/x", "mode": "0700"}}
    assert chmods_after_the_acl([acl, chmod]) == ["Reset"]
