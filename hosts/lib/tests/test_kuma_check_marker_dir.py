"""The retired `/var/lib/kuma-check/` marker directory: each host package's legacy entries remove it.

Run: uv run pytest hosts/lib/tests/test_kuma_check_marker_dir.py
"""

import json

import pytest
from _reconcile_host import Node

from renderer.renderer_lib import host as host_mode

DIRECTORY = "/var/lib/kuma-check"
MARKERS = ("github-ruleset-drift.down", "github-interaction-limit.down")


def kuma_check_legacy(host: str) -> list[dict]:
    manifest = json.loads(
        host_mode.render_host(host).files["/usr/lib/homelab/manifest.json"][0]
    )
    return [e for e in manifest["legacy"] if e.get("path", "").startswith(DIRECTORY)]


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server", "daniel-pi"])
def test_each_host_package_retires_the_marker_directory(host):
    assert {"path": DIRECTORY} in kuma_check_legacy(host)


def test_a_pi_marker_written_after_its_entry_retired_goes_with_the_directory(
    tmp_path,
):
    legacy = kuma_check_legacy("daniel-pi")
    node = Node(tmp_path)
    node.manifest["handoffs"] = []
    node.manifest["legacy"] = [{"path": f"{DIRECTORY}/{name}"} for name in MARKERS]
    first = node.run()
    assert first.returncode == 0, first.stderr
    stale = node.root / DIRECTORY.lstrip("/")
    stale.mkdir(parents=True)
    for name in MARKERS:
        (stale / name).write_text("")
    node.manifest["legacy"] = legacy

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
