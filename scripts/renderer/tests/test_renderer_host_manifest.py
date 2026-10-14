"""The manifest a host package carries: the schema reconcile parses, and what feeds it.

Each render reads a fixture tree under `tmp_path`, written by `conftest.py`'s `hosts`.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_manifest.py
"""

import importlib.machinery
import json
import types

import pytest

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.host_manifest import MANIFEST


UNITS = "/usr/lib/systemd/system"


def _reconcile():
    """`hosts/lib/homelab-reconcile` as a module, so its own parser judges the manifest."""
    path = REPO / "hosts" / "lib" / "homelab-reconcile"
    loader = importlib.machinery.SourceFileLoader("homelab_reconcile", str(path))
    module = types.ModuleType(loader.name)
    loader.exec_module(module)
    return module


def _manifest(package) -> dict:
    return json.loads(package.files[MANIFEST][0])


def test_a_host_manifest_lists_units_links_policies_and_legacy_entries(hosts):
    component = {
        "files": [
            {"src": "u", "dst": f"{UNITS}/homelab-beat.service"},
            {"src": "u", "dst": f"{UNITS}/homelab-beat.timer"},
            {"src": "u", "dst": f"{UNITS}/timers.target.wants/homelab-beat.timer"},
            {"src": "u", "dst": f"{UNITS}/homelab-updater.timer.d/50-homelab.conf"},
        ],
        "units": {"homelab-beat.service": {"policy": "manual"}},
        "legacy": [
            {"path": "/usr/local/bin/beat.sh"},
            {"cron": "Beat heartbeat", "user": "{{ sys_user }}"},
            {"cron_file": "beat"},
            {"unit": "beat.service"},
        ],
    }
    root = hosts({"components": ["beat"]}, {"beat": component}, {"u": ""})
    updater = hosts(
        {"files": [{"src": "u", "dst": f"{UNITS}/homelab-updater.service"}]},
        where="packages/homelab-updater",
    )
    (updater / "packages" / "homelab-updater" / "u").write_text("")

    package = host_mode.render_host("daniel-pi", root)

    text = package.files[MANIFEST][0].decode()
    parsed = _reconcile().parse_manifest(text)
    assert parsed == {
        "units": {
            "homelab-beat.service": {"policy": "manual"},
            "homelab-beat.timer": {},
            "homelab-updater.service": {},
            "homelab-updater.timer": {},
        },
        "enable_links": ["homelab-beat.timer"],
        "legacy": [
            {"path": "/usr/local/bin/beat.sh"},
            {"cron": "Beat heartbeat", "user": "ubuntu"},
            {"cron_file": "beat"},
            {"unit": "beat.service"},
        ],
        "reconcilers": [],
    }
    assert parsed["legacy"] == package.legacy


def test_the_real_updater_s_units_join_a_host_manifest(tmp_path):
    hosts = tmp_path / "hosts"
    (hosts / "packages").mkdir(parents=True)
    (hosts / "packages" / "homelab-updater").symlink_to(
        REPO / "hosts/packages/homelab-updater"
    )
    (hosts / "components").mkdir()
    (hosts / "components" / "reconcile").symlink_to(REPO / "hosts/components/reconcile")
    (hosts / "lib").symlink_to(REPO / "hosts/lib")
    (hosts / "daniel-pi").mkdir()
    (hosts / "daniel-pi" / "package.yml").write_text("components: [reconcile]\n")

    units = _manifest(host_mode.render_host("daniel-pi", hosts))["units"]

    assert {
        "homelab-reconcile.service",
        "homelab-updater.service",
        "homelab-updater.timer",
    } <= set(units)


def test_a_shared_package_ships_no_manifest(hosts):
    root = hosts({"description": "x"}, where="packages/homelab-updater")
    assert host_mode.render_package("homelab-updater", root).files == {}


@pytest.mark.parametrize(
    ("package", "where", "message"),
    [
        (
            {"legacy": [{"path": "/usr/local/bin/x"}]},
            "packages/homelab-updater",
            "a shared package lists no legacy entry",
        ),
        (
            {"units": {"homelab-gone.timer": {"policy": "manual"}}},
            "daniel-pi",
            "no package installs them",
        ),
        (
            {
                "files": [
                    {"src": "u", "dst": f"{UNITS}/multi-user.target.wants/ssh.service"}
                ]
            },
            "daniel-pi",
            r"enable links name \['ssh.service'\]",
        ),
        (
            {"files": [{"src": "u", "dst": f"{UNITS}/foo.mount.d/50-homelab.conf"}]},
            "daniel-pi",
            r"drop-ins target \['foo.mount'\]",
        ),
    ],
)
def test_a_manifest_entry_no_package_backs_is_flagged(hosts, package, where, message):
    root = hosts(package, where=where)
    (root / where / "u").write_text("")
    render = host_mode.render_host if where == "daniel-pi" else host_mode.render_package
    with pytest.raises(RenderRefused, match=message):
        render(where.rsplit("/", 1)[-1], root)


def test_a_legacy_entry_reading_a_secret_is_flagged(hosts):
    root = hosts({"legacy": [{"path": "/srv/{{ email }}/x"}]})
    with pytest.raises(RenderError, match="email is a secret key"):
        host_mode.render_host("daniel-pi", root)
