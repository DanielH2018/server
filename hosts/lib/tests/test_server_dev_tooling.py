"""daniel-server's dev tooling moves into its package, and `/usr/bin/uv` reaches both cluster hosts (H7.61).

The `uv` and `vale` components put the homelab-tools binaries on PATH, and the /usr/local/bin
copy of Vale that sits ahead of its binary is retired on this host, as is a stray
/usr/local/bin/prek the dotfiles repo's prek replaces. The worktree holder scan's half
of the row is
`ansible/tests/setup/test_worktree_holders_install.py`.

Run: uv run pytest hosts/lib/tests/test_server_dev_tooling.py
"""

import json

import pytest

from deploy_tools.setup_lib.setup_gates import packaged_guard_hosts
from lib import yaml_fast
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
HOST = "daniel-server"


@pytest.fixture(scope="module")
def server() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def _legacy_paths(package: host_mode.Package) -> set[str]:
    manifest = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])
    return {entry.get("path") for entry in manifest["legacy"]}


def _wrapper_execs(package: host_mode.Package, tool: str) -> bool:
    body, mode, *_ = package.files[f"/usr/bin/{tool}"]
    return mode == 0o755 and f'exec /usr/lib/homelab/bin/{tool} "$@"' in body.decode()


def test_uv_reaches_both_hosts(server, box):
    assert _wrapper_execs(server, "uv")
    assert _wrapper_execs(box, "uv")
    assert "homelab-tools" in server.depends and "homelab-tools" in box.depends


def test_vale_reaches_daniel_server_and_retires_the_old_copy(server):
    assert _wrapper_execs(server, "vale")
    assert "/usr/local/bin/vale" in _legacy_paths(server)


def test_the_stray_root_prek_is_retired_on_daniel_server(server):
    assert "/usr/local/bin/prek" in _legacy_paths(server)


def _task(name: str) -> dict:
    tasks = yaml_fast.safe_load((ROLE / "tasks" / "host-basics.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_the_ansible_tools_keep_running_on_daniel_server():
    # ansible-core and ansible-lint stay until H7.63, and only prek leaves.
    install = _task("Install Python CLI tooling as uv tools")
    assert [item.partition("==")[0] for item in install["loop"]] == [
        "ansible-core",
        "ansible-lint",
    ]
    assert packaged_guard_hosts([install["when"]], (HOST,), ROLE) == frozenset()
    assert install["ansible.builtin.command"].startswith("/usr/bin/uv tool install ")
