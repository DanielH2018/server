"""The worktree sweep's root helper is installed where its socket and its callers look.

`lib.worktrees.privileged_holders` and `fanout_lib.clean.live_process_scan` connect to
`WORKTREE_HOLDERS_SOCKET`; the socket unit listens there, and its per-connection service runs
the helper the package installs. If those drift apart, every caller falls back to the
unprivileged scan, or every removal is refused. The root map both the helper and the caller
read names each user's worktree root, and it must name every agent user a host package
declares (#4295, #6831). Every host package retires the sudo grant the socket replaced (#4297).

Both hosts get all of it from the `worktree-holders` component (H7.61, H7.62).

Run: uv run pytest hosts/tests/test_worktree_holders_install.py
"""

import json

import pytest
from _helpers import HOST_VARS, REPO
from lib import yaml_fast
from lib.worktrees import WORKTREE_HOLDER_ROOTS, WORKTREE_HOLDERS_SOCKET
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "worktree-holders" / "component.yml"
UNITS = "/usr/lib/systemd/system"
PACKAGED_HELPER = "/usr/lib/homelab/worktree-holders"
# What the role wrote on the host, each of which reconcile must retire.
RETIRED_PATHS = {
    "/etc/systemd/system/worktree-holders.socket",
    "/etc/systemd/system/worktree-holders@.service",
    "/etc/systemd/system/sockets.target.wants/worktree-holders.socket",
    "/usr/local/libexec/worktree-holders",
    "/var/lib/homelab/setup-deployed-manifest.d/worktree-holders",
}
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


def _only_the_group_may_connect(socket: dict) -> bool:
    return socket.get("SocketMode") == "0660" and socket.get("SocketUser") == "root"


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


OLD_SUDOERS = "/etc/sudoers.d/20-worktree-holders"


def _retires_the_old_sudoers_rule(legacy: list[dict]) -> bool:
    return {"path": OLD_SUDOERS} in legacy


def test_a_package_that_keeps_the_old_sudoers_rule_is_flagged():
    assert not _retires_the_old_sudoers_rule([{"path": "/usr/local/bin/uv"}])


@pytest.fixture(scope="module", params=sorted(KNOWN_CLAUDE_HOSTS))
def package(request) -> tuple[str, host_mode.Package]:
    return request.param, host_mode.render_host(request.param)


def test_every_claude_host_package_retires_the_old_sudoers_rule_is_clean(package):
    # #4297: a host that had the sudo grant keeps a NOPASSWD root rule unless something
    # deletes it, and no setup task does since H7.60.
    assert _retires_the_old_sudoers_rule(package[1].legacy)


def test_the_pi_package_retires_the_old_sudoers_rule_is_clean():
    assert _retires_the_old_sudoers_rule(host_mode.render_host("daniel-pi").legacy)


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _socket_and_service(package: host_mode.Package) -> tuple[dict, dict]:
    return (
        _directives(_text(package, f"{UNITS}/worktree-holders.socket")),
        _directives(_text(package, f"{UNITS}/worktree-holders@.service")),
    )


def test_the_packaged_socket_and_service_meet_the_callers_is_clean(package):
    _, rendered = package
    socket, service = _socket_and_service(rendered)

    assert _units_meet_the_callers(socket, service, PACKAGED_HELPER)
    assert _only_the_group_may_connect(socket)
    assert socket["SocketGroup"] == "worktree-holders"
    assert rendered.files[PACKAGED_HELPER][1] == 0o700


def test_a_service_whose_stdout_reaches_the_socket_is_flagged(package):
    socket, service = _socket_and_service(package[1])

    assert not _units_meet_the_callers(
        socket, {**service, "StandardOutput": "socket"}, PACKAGED_HELPER
    )
    assert not _units_meet_the_callers(
        {**socket, "ListenStream": "/run/elsewhere.sock"}, service, PACKAGED_HELPER
    )


def test_a_world_writable_socket_is_flagged(package):
    socket, _ = _socket_and_service(package[1])

    assert not _only_the_group_may_connect({**socket, "SocketMode": "0666"})


def test_the_packaged_socket_makes_its_group_before_systemd_binds(package):
    # SocketGroup= fails the socket on a host where the group does not exist yet.
    lines = _text(package[1], f"{UNITS}/worktree-holders.socket").splitlines()
    assert [line for line in lines if line.startswith("ExecStartPre=")] == [
        "ExecStartPre=+/usr/sbin/groupadd -f -r worktree-holders",
        "ExecStartPre=+/usr/sbin/usermod -aG worktree-holders ubuntu",
    ]


def test_the_packaged_root_map_names_the_operator_and_the_package_agents(package):
    # #6831: each host's map matches its package's `users:` entry. The map is where the service
    # looks a caller up, so an agent missing from it is refused every scan.
    host, rendered = package
    packaged = json.loads(_text(rendered, WORKTREE_HOLDER_ROOTS))

    assert packaged == {
        "ubuntu": "/home/ubuntu/server/.claude/worktrees",
        **_package_agent_roots()[host],
    }


def test_reconcile_retires_what_the_role_installed(package):
    _, rendered = package
    manifest = json.loads(rendered.files["/usr/lib/homelab/manifest.json"][0])

    assert RETIRED_PATHS <= {e.get("path") for e in manifest["legacy"]}
    assert "worktree-holders.socket" in manifest["enable_links"]
    assert "netcat-openbsd" in rendered.depends
    # The way out: every installed path follows the flag the role's teardown read.
    spec = yaml_fast.safe_load(COMPONENT.read_text())
    assert {f.get("when") for f in spec["files"]} == {"has_claude_code"}
    assert spec["enable"]["worktree-holders.socket"]["when"] == "has_claude_code"
