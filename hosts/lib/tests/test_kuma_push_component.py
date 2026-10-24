"""The `kuma-push` component, and the renderer's refusal of one path from two components (H4.3b1).

`kuma-push` owns `/usr/lib/homelab/kuma-push-lib.sh` in every package that lists it. The Pi's
`pi-heartbeats` owned it until H4.3b1. A package in which two components ship one path would
leave the second to win by list order, so the renderer refuses it.

Run: uv run pytest hosts/lib/tests/test_kuma_push_component.py
"""

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused

LIB = "/usr/lib/homelab/kuma-push-lib.sh"
SOURCE = REPO / "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh"


def _component(name: str) -> dict:
    return yaml.safe_load(
        (REPO / "hosts/components" / name / "component.yml").read_text()
    )


def _dsts(component: dict) -> list[str]:
    return [entry["dst"] for entry in component.get("files", [])]


@pytest.mark.parametrize("host", ["daniel-pi", "daniel-server"])
def test_each_host_package_ships_the_push_helper_as_a_byte_copy(host):
    package = host_mode.render_host(host)
    assert package.files[LIB] == (SOURCE.read_bytes(), 0o644)


def test_the_component_ships_the_helper_and_pi_heartbeats_no_longer_does():
    assert _dsts(_component("kuma-push")) == [LIB]
    assert LIB not in _dsts(_component("pi-heartbeats"))


def _two_components(tmp_path, second_dst: str):
    root = tmp_path / "hosts"
    (root / "daniel-pi").mkdir(parents=True)
    (root / "daniel-pi" / "package.yml").write_text(
        yaml.safe_dump({"components": ["one", "two"]})
    )
    for name, dst in (("one", LIB), ("two", second_dst)):
        directory = root / "components" / name
        directory.mkdir(parents=True)
        (directory / "x").write_text("x\n")
        (directory / "component.yml").write_text(
            yaml.safe_dump({"files": [{"src": "x", "dst": dst}]})
        )
    return root


def test_two_components_shipping_one_path_are_refused(tmp_path):
    root = _two_components(tmp_path, LIB)
    with pytest.raises(RenderRefused, match=f"two files install {LIB}"):
        host_mode.render_host("daniel-pi", root)


def test_two_components_shipping_different_paths_render(tmp_path):
    root = _two_components(tmp_path, "/usr/lib/homelab/other")
    package = host_mode.render_host("daniel-pi", root)
    assert {LIB, "/usr/lib/homelab/other"} <= set(package.files)
