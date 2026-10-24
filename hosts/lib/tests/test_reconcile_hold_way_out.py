"""The way out of a hold: a uid mismatch holds the host, and only a fixed version clears it (H7.2d).

*Which outcomes hold the host* (docs/target-architecture/host/h7-delete-ansible.md) ends in a
procedure: install the fix or the revert, then run `homelab-updater --clear-hold`. This drives
the real updater with the real reconcile. `_updater_stub.py`'s `reconcile_run` makes the
updater's `systemctl start homelab-reconcile.service` run reconcile against the fake host in
`_reconcile_host.py`, and the updater reads the record that run wrote. The two fake hosts share
no directory, so each test sets the same package versions in both.

Run: uv run pytest hosts/lib/tests/test_reconcile_hold_way_out.py
"""

import pytest
from _reconcile_arms import base
from _reconcile_host import Node
from _updater_host import V1, V2, Host
from _updater_host import reconcile_ok as _ok

V3 = "0.102+gcccccccccccc"
CLAUDE = {
    "name": "claude",
    "uid": 1001,
    "home": "/home/claude",
    "shell": "/bin/bash",
    "comment": "Claude agent",
}


@pytest.fixture
def pair(tmp_path) -> tuple[Host, Node]:
    for name in ("updater", "reconcile"):
        (tmp_path / name).mkdir()
    host = Host(tmp_path / "updater")
    node = base(Node(tmp_path / "reconcile"))
    node.manifest["reconcilers"] = ["users"]
    node.state["users"] = {"claude": 1000}  # the live uid; the package declares 1001
    node.manifest["users"] = {"users": [CLAUDE]}
    # Both hosts start from the record the version-1 run left, as one host would.
    node.applied_path.parent.mkdir(parents=True)
    node.applied_path.write_text(host.record.read_text())
    return host, node


def at_version(host: Host, node: Node, version: str, *, publish: bool = True) -> None:
    """Make `version` the one the updater installs next, and the one reconcile reads as installed."""
    if publish:
        host.state["repo"] = {
            "homelab-updater": [V1, version],
            "homelab-daniel-pi": [V1, version],
        }
    for package in ("homelab-updater", "homelab-daniel-pi"):
        node.state["installed"][package] = ["ii ", version]
    argv, env = node.command()
    host.state["reconcile_run"] = {
        "argv": argv,
        "env": env,
        "record": str(node.applied_path),
    }


def held_by_a_uid_mismatch(host: Host, node: Node) -> None:
    host.succeeded_before()
    at_version(host, node, V2)

    result = host.run()

    assert result.returncode == 1
    assert host.state["installed"]["homelab-daniel-pi"] == ["ii ", V2]
    assert host.hold.exists()
    assert _ok(host.metrics()) == "0"
    assert f'version="{V1}"' in host.metrics()["homelab_host_applied_info"]


def test_a_declared_uid_that_differs_from_the_live_uid_holds_the_host(pair):
    held_by_a_uid_mismatch(*pair)


def test_installing_a_version_that_declares_the_live_uid_and_clearing_the_hold_releases_it(
    pair,
):
    host, node = pair
    held_by_a_uid_mismatch(host, node)
    node.manifest["users"] = {"users": [{**CLAUDE, "uid": 1000}]}
    at_version(host, node, V3)

    result = host.run("--clear-hold", "--install-newest")

    assert result.returncode == 0, result.stderr
    assert host.state["installed"]["homelab-daniel-pi"] == ["ii ", V3]
    assert not host.hold.exists()
    assert _ok(host.metrics()) == "1"
    assert f'version="{V3}"' in host.metrics()["homelab_host_applied_info"]


def test_clearing_the_hold_with_nothing_installed_keeps_it(pair):
    host, node = pair
    held_by_a_uid_mismatch(host, node)
    at_version(host, node, V2, publish=False)  # reconcile reruns on the same manifest

    result = host.run("--clear-hold")

    assert result.returncode == 1
    assert host.state["installed"]["homelab-daniel-pi"] == ["ii ", V2]
    assert host.hold.exists()
    assert _ok(host.metrics()) == "0"
    assert f'version="{V1}"' in host.metrics()["homelab_host_applied_info"]
