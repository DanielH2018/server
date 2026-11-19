"""The `kuma-check-dir` component: each host removes `/var/lib/kuma-check/` and any marker in it.

Run: uv run pytest hosts/lib/tests/test_kuma_check_marker_dir.py
"""

import json

import pytest
import yaml
from _reconcile_host import Node

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

DIRECTORY = "/var/lib/kuma-check"
HOSTS = ("daniel-box", "daniel-server", "daniel-pi")
COMPONENT = REPO / "hosts/components/kuma-check-dir/component.yml"


def kuma_check_legacy(host: str) -> list[dict]:
    manifest = json.loads(
        host_mode.render_host(host).files["/usr/lib/homelab/manifest.json"][0]
    )
    return [e for e in manifest["legacy"] if e.get("path", "").startswith(DIRECTORY)]


def marker_names() -> list[str]:
    paths = [e["path"] for e in yaml.safe_load(COMPONENT.read_text())["legacy"]]
    return [p.rsplit("/", 1)[1] for p in paths if p != DIRECTORY]


@pytest.mark.parametrize("host", HOSTS)
def test_the_directory_is_the_last_kuma_check_entry_each_host_renders(host):
    assert kuma_check_legacy(host)[-1] == {"path": DIRECTORY}


@pytest.mark.parametrize("host", HOSTS)
def test_a_marker_written_after_its_entry_retired_goes_with_the_directory(
    host, tmp_path
):
    legacy = kuma_check_legacy(host)
    node = Node(tmp_path)
    node.manifest["handoffs"] = []
    node.manifest["legacy"] = [
        {"path": f"{DIRECTORY}/{name}"} for name in marker_names()
    ]
    first = node.run()
    assert first.returncode == 0, first.stderr
    stale = node.root / DIRECTORY.lstrip("/")
    stale.mkdir(parents=True)
    for name in marker_names():
        (stale / name).write_text("")
    node.manifest["legacy"] = legacy

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
