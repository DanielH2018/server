"""The worktree sweep's root helper is installed where its socket and its callers look.

`lib.worktrees.privileged_holders` and `fanout_lib.clean.live_process_scan` connect to
`WORKTREE_HOLDERS_SOCKET`; the socket unit listens there, and its per-connection service runs
the helper the role installs. If those drift apart, every caller falls back to the
unprivileged scan, or every removal is refused. The root map both the helper and the caller
read names each user's worktree root, and it must name every agent user a host package
declares (#4295, #6831). The sudo grant the socket replaced must be gone from every host (#4297).

Run: uv run pytest ansible/tests/setup/test_worktree_holders_install.py
"""

import json
from pathlib import PurePosixPath

from _helpers import (
    HOST_VARS,
    REPO,
    SETUP_ROLES,
    load_tasks,
    render_expr,
    task_named,
)
from _setup_render import render_setup_text, rendered_setup_text
from lib import yaml_fast
from lib.worktrees import WORKTREE_HOLDER_ROOTS, WORKTREE_HOLDERS_SOCKET

CRONS = SETUP_ROLES / "initial_setup" / "tasks" / "crons.yml"
HELPER = "/usr/local/libexec/worktree-holders"
INSTALL = "Install the root-run worktree holder scan"
AGENTS = "Name the agent users granted the worktree holder scan"
USERS = "List the worktree holder scan's users"
ROOT_MAP = "Map each user of the worktree holder scan"
SUDOERS = "Remove the worktree holder scan's old sudoers rule"
GROUP = "initial_setup_worktree_holders_group"
# A has_claude_code host the agent-list copy must cover, so an empty host census fails.
KNOWN_CLAUDE_HOSTS = {"daniel-box", "daniel-server"}


def _directives(text: str) -> dict[str, str]:
    """`Key=Value` for every directive line of a rendered unit. A repeated key keeps its last."""
    found = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def _socket_and_service() -> tuple[dict[str, str], dict[str, str]]:
    return (
        _directives(rendered_setup_text("initial_setup", "worktree-holders.socket.j2")),
        _directives(
            rendered_setup_text("initial_setup", "worktree-holders@.service.j2")
        ),
    )


def _units_meet_the_callers(socket: dict, service: dict, installed: str) -> bool:
    """The socket listens where the callers connect, and its service runs the installed helper.

    The answer reaches the caller only through the helper's own write: stdout and stderr go to
    the journal, so a traceback never arrives where the caller reads an answer.
    """
    return (
        socket.get("ListenStream") == WORKTREE_HOLDERS_SOCKET
        and socket.get("Accept") == "yes"
        and service.get("ExecStart") == installed
        and service.get("StandardInput") == "socket"
        and service.get("StandardOutput") == "journal"
        and service.get("StandardError") == "journal"
    )


def test_the_socket_listens_where_the_callers_connect_and_runs_the_helper_is_clean():
    install = task_named(load_tasks(CRONS), INSTALL)["ansible.builtin.copy"]
    socket, service = _socket_and_service()

    assert install["dest"] == HELPER
    assert _units_meet_the_callers(socket, service, install["dest"])


def test_a_service_whose_stdout_reaches_the_socket_is_flagged():
    socket, service = _socket_and_service()

    assert not _units_meet_the_callers(
        socket, {**service, "StandardOutput": "socket"}, HELPER
    )
    assert not _units_meet_the_callers(
        {**socket, "ListenStream": "/run/elsewhere.sock"}, service, HELPER
    )


def _only_the_group_may_connect(socket: dict) -> bool:
    return socket.get("SocketMode") == "0660" and socket.get("SocketUser") == "root"


def test_only_the_sockets_group_may_connect_is_clean():
    # The operator's ruling (2026-10-10): a group-restricted socket mode decides who may
    # connect. The group is the role's variable, the one the claude-code
    # component's claude-agent-setup gives each agent user.
    socket, _ = _socket_and_service()
    moved = _directives(
        render_setup_text(
            "initial_setup", "worktree-holders.socket.j2", {GROUP: "sentinel-group"}
        )
    )

    assert _only_the_group_may_connect(socket)
    assert moved["SocketGroup"] == "sentinel-group"


def test_a_world_writable_socket_is_flagged():
    socket, _ = _socket_and_service()

    assert not _only_the_group_may_connect({**socket, "SocketMode": "0666"})


def _parent_created_before_install(tasks: list[dict], dest: str) -> bool:
    parent = str(PurePosixPath(dest).parent)
    for task in tasks:
        if task.get("ansible.builtin.copy", {}).get("dest") == dest:
            return False
        made = task.get("ansible.builtin.file", {})
        if made.get("path") == parent and made.get("state") == "directory":
            return True
    raise AssertionError(f"no task installs {dest}")


def test_the_install_directory_is_created_before_the_copy_is_clean():
    # `copy` does not create a missing parent, and Ubuntu ships no /usr/local/libexec. The first
    # apply failed on daniel-box for exactly that and held the initial_setup plane.
    assert _parent_created_before_install(load_tasks(CRONS), HELPER)


def test_an_install_with_no_directory_task_before_it_is_flagged():
    install = {"ansible.builtin.copy": {"dest": HELPER}}
    late_dir = {
        "ansible.builtin.file": {
            "path": str(PurePosixPath(HELPER).parent),
            "state": "directory",
        }
    }
    assert not _parent_created_before_install([install, late_dir], HELPER)


def _rendered_roots(tasks: list[dict], host: str) -> dict[str, str]:
    """The root map the role writes on `host`, from its own per-host copy of the agent users."""
    agents = task_named(tasks, AGENTS)
    users = task_named(tasks, USERS)["ansible.builtin.set_fact"]
    context = {"sys_user": "ubuntu", "inventory_hostname": host, **agents["vars"]}
    context["initial_setup_worktree_holders_agents"] = render_expr(
        agents["ansible.builtin.set_fact"]["initial_setup_worktree_holders_agents"],
        **context,
    )
    context["initial_setup_worktree_holders_users"] = render_expr(
        users["initial_setup_worktree_holders_users"], **context
    )
    context["initial_setup_worktree_holders_checkouts"] = render_expr(
        users["initial_setup_worktree_holders_checkouts"], **context
    )
    text = render_expr(
        task_named(tasks, ROOT_MAP)["ansible.builtin.copy"]["content"], **context
    )
    return json.loads(text) if isinstance(text, str) else text


def _package_agent_roots() -> dict[str, dict[str, str]]:
    """Each has_claude_code host mapped to {agent user: its worktree root}, from its package."""
    out = {}
    for host_vars in sorted(HOST_VARS.glob("*.yml")):
        if not (yaml_fast.safe_load(host_vars.read_text()) or {}).get(
            "has_claude_code"
        ):
            continue
        package = REPO / "hosts" / host_vars.stem / "package.yml"
        users = (yaml_fast.safe_load(package.read_text()) or {}).get("users", [])
        out[host_vars.stem] = {
            u["name"]: f"{u['home']}/server/.claude/worktrees" for u in users
        }
    return out


def _covers_every_package_agent(
    tasks: list[dict], packages: dict[str, dict[str, str]]
) -> bool:
    """Whether each host's root map names exactly its package's agent users, at their clones."""
    return all(
        {u: r for u, r in _rendered_roots(tasks, host).items() if u != "ubuntu"}
        == declared
        for host, declared in packages.items()
    )


def test_the_root_map_names_the_operator_and_every_package_agent_is_clean():
    # #6831: each host's copy of the agent users matches its package's `users:` entry. The map
    # is where the service looks a caller up, so an agent missing from it is refused every scan.
    tasks = load_tasks(CRONS)
    copy = task_named(tasks, ROOT_MAP)["ansible.builtin.copy"]
    packages = _package_agent_roots()

    assert (copy["dest"], copy["owner"]) == (WORKTREE_HOLDER_ROOTS, "root")
    assert set(packages) >= KNOWN_CLAUDE_HOSTS
    assert _rendered_roots(tasks, "daniel-box")["ubuntu"] == (
        "/home/ubuntu/server/.claude/worktrees"
    )
    assert _covers_every_package_agent(tasks, packages)


def test_a_package_agent_missing_from_the_copy_is_flagged():
    packages = _package_agent_roots()
    packages["daniel-box"] = {
        **packages["daniel-box"],
        "claude2": "/var/lib/claude2/server/.claude/worktrees",
    }

    assert not _covers_every_package_agent(load_tasks(CRONS), packages)


def test_an_agent_one_host_switched_off_but_the_copy_still_grants_is_flagged():
    # Switching the agent off on one host deletes its `users:` entry there; the map must follow.
    packages = _package_agent_roots()
    packages["daniel-server"] = {}

    assert not _covers_every_package_agent(load_tasks(CRONS), packages)


def _sudoers_removed_everywhere(tasks: list[dict]) -> bool:
    task = task_named(tasks, SUDOERS)
    return (
        task.get("ansible.builtin.file")
        == {"path": "/etc/sudoers.d/20-worktree-holders", "state": "absent"}
        and "when" not in task
    )


def test_the_old_sudoers_rule_is_removed_on_every_host_is_clean():
    # #4297: a host that had the sudo grant keeps a NOPASSWD root rule unless something
    # deletes it. The has_claude_code hosts are exactly the ones that have it.
    tasks = load_tasks(CRONS)

    assert _sudoers_removed_everywhere(tasks)
    assert not any(
        "sudoers" in str(t.get("ansible.builtin.copy", {}).get("dest", ""))
        for t in tasks
    )


def test_a_sudoers_removal_only_off_claude_hosts_is_flagged():
    tasks = load_tasks(CRONS)
    task = task_named(tasks, SUDOERS)
    gated = [{**t, "when": "not has_claude_code"} if t is task else t for t in tasks]

    assert not _sudoers_removed_everywhere(gated)
