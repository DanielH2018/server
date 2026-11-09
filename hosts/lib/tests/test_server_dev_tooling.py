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

from renderer.renderer_lib import host as host_mode

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
