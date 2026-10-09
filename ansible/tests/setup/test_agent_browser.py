#!/usr/bin/env python3
"""The agent user launches its own homelab-ui MCP server, and switching it off removes it.

`roles/setup/claude_code/tasks/agent_browser.yml` gives the `claude` agent user Node,
`@playwright/mcp`, Chromium and a user-scope `homelab-ui` registration (#4058). The operator's
toolchain sits under a home the agent cannot read, so a registration that points anywhere but
the agent's own clone and Node fails only when a phone session first calls the server.

Run: uv run pytest ansible/tests/setup/test_agent_browser.py
"""

import json

from _helpers import ANSIBLE
from lib import yaml_fast

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
BROWSER = ROLE / "tasks" / "agent_browser.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
LAUNCHER = ANSIBLE.parent / "scripts" / "diagnostics" / "ui_mcp.sh"
SWITCH = "claude_code_agent_homelab_ui_enabled"


def tasks() -> list[dict]:
    return yaml_fast.safe_load(BROWSER.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def registration(task_list: list[dict]) -> dict:
    return named(
        task_list,
        "Compare the agent user's homelab-ui registration with the one it should have",
    )["ansible.builtin.set_fact"]["claude_code_agent_homelab_ui_mcp"]


def when_list(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else when


def test_the_registration_launches_the_agents_clone_with_the_agents_node() -> None:
    """ui_mcp.sh defaults NODE_BIN to the operator's fnm path, which the agent has none of."""
    entry = registration(tasks())
    assert entry["command"] == (
        "{{ claude_code_agent_user_clone_dir }}/scripts/diagnostics/ui_mcp.sh"
    )
    assert LAUNCHER.exists()
    assert entry["env"] == {"NODE_BIN": "{{ claude_code_agent_node_bin }}"}
    assert 'NODE_BIN="${NODE_BIN:-' in LAUNCHER.read_text(), (
        "ui_mcp.sh no longer takes NODE_BIN from the environment"
    )
    defaults = yaml_fast.safe_load(DEFAULTS.read_text())
    assert defaults["claude_code_agent_node_bin"] == (
        "{{ claude_code_agent_node_dir }}/current/bin"
    )
    assert defaults[SWITCH] is False


def test_the_registration_is_user_scope_and_added_only_when_switched_on() -> None:
    """A project-scope server waits for an approval a phone session cannot give."""
    add = named(tasks(), "Register the homelab-ui MCP server for the agent user")
    argv = add["ansible.builtin.command"]["argv"]
    assert argv[argv.index("--scope") + 1] == "user"
    assert SWITCH in when_list(add)


def off_arm_gaps(task_list: list[dict]) -> list[str]:
    """What the switched-off arm leaves behind of what the on arm installs."""
    gaps = []
    remove = named(
        task_list,
        "Remove the agent user's homelab-ui registration when it is stale or switched off",
    )
    if not any(f"not {SWITCH}" in w for w in when_list(remove)):
        gaps.append("registration")
    wipe = named(
        task_list,
        "Remove the agent user's browser toolchain when homelab-ui is switched off",
    )
    paths = wipe.get("loop", []) if f"not {SWITCH}" in when_list(wipe) else []
    for path in (
        "{{ claude_code_agent_node_dir }}",
        "{{ claude_code_agent_user_home }}/.cache/ms-playwright",
    ):
        if path not in paths:
            gaps.append(path)
    download = named(
        task_list,
        "Remove the agent user's Node download when homelab-ui is switched off",
    )
    if f"not {SWITCH}" not in when_list(download):
        gaps.append("download")
    return gaps


def test_switching_homelab_ui_off_removes_everything_switching_it_on_installed() -> (
    None
):
    assert off_arm_gaps(tasks()) == []


def test_an_off_arm_that_keeps_the_browser_cache_is_flagged() -> None:
    live = json.loads(json.dumps(tasks()))
    wipe = named(
        live,
        "Remove the agent user's browser toolchain when homelab-ui is switched off",
    )
    wipe["loop"] = ["{{ claude_code_agent_node_dir }}"]
    assert off_arm_gaps(live) == [
        "{{ claude_code_agent_user_home }}/.cache/ms-playwright"
    ]


# Variables that name a path inside the agent's home, which the agent owns.
AGENT_PATH_VARS = ("claude_code_agent_user_home", "claude_code_agent_node_dir")


def root_modules_on_agent_paths(task_list: list[dict]) -> list[str]:
    """Every task that touches an agent-owned path with a module other than `runuser`.

    A root-run module acts on whatever the agent left at the path, a symlink to a system
    directory included. Only a command whose argv starts with `runuser` is exempt.
    """
    flagged = []
    for t in task_list:
        for key, args in t.items():
            if not key.startswith("ansible.builtin.") or not isinstance(args, dict):
                continue
            if (
                key == "ansible.builtin.command"
                and (args.get("argv") or [None])[0] == "runuser"
            ):
                continue
            if key == "ansible.builtin.set_fact":
                continue
            text = json.dumps(args)
            if any(var in text for var in AGENT_PATH_VARS):
                flagged.append(str(t.get("name")))
    return flagged


def test_root_never_runs_a_module_on_a_path_the_agent_owns() -> None:
    task_list = tasks()
    named(task_list, "Unpack the agent user's Node")
    assert root_modules_on_agent_paths(task_list) == []


def test_a_root_run_module_on_an_agent_path_is_flagged() -> None:
    task = {
        "name": "Create it",
        "ansible.builtin.file": {
            "path": "{{ claude_code_agent_node_dir }}",
            "state": "directory",
        },
    }
    as_agent = {
        "name": "Create it as the agent",
        "ansible.builtin.command": {
            "argv": [
                "runuser",
                "-u",
                "claude",
                "--",
                "mkdir",
                "{{ claude_code_agent_node_dir }}",
            ]
        },
    }
    assert root_modules_on_agent_paths([task, as_agent]) == ["Create it"]


def env_commands_without_tmpdir(task_list: list[dict]) -> tuple[list[str], list[str]]:
    """Every `runuser ... -- env ...` command, and those of them that set no TMPDIR.

    `runuser` keeps the caller's environment, so without its own TMPDIR the agent inherits
    root's per-user /tmp/user/0 and cannot create a temp file: the Chromium download failed
    there with EACCES on mkdtemp. CI sets no per-user TMPDIR, so only the host shows it.
    """
    checked, missing = [], []
    for t in task_list:
        argv = (t.get("ansible.builtin.command") or {}).get("argv") or []
        if not argv or argv[0] != "runuser" or "env" not in argv:
            continue
        name = str(t.get("name"))
        checked.append(name)
        env_args = argv[argv.index("env") + 1 :]
        if not any(str(a).startswith("TMPDIR=") for a in env_args):
            missing.append(name)
    return checked, missing


def test_every_runuser_env_command_gives_the_agent_its_own_tmpdir() -> None:
    checked, missing = env_commands_without_tmpdir(tasks())
    for name in (
        "Install the agent user's pinned @playwright/mcp",
        "Install the Chromium the agent user's @playwright/mcp pins",
    ):
        assert name in checked, f"{name!r} is no longer a runuser env command"
    assert missing == []
