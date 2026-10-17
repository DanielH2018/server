"""The standalone-value guard's committed map covers every standalone use (target slice R2).

The deploy path reads `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`, not the
render, so a template change that adds a standalone use would leave the map short. This test
renders the fleet in placeholder mode once and holds the map to it.

Run: uv run pytest scripts/renderer/tests/test_renderer_standalone_map.py
"""

import pytest

from renderer import homelab_render
from renderer.renderer_lib.placeholders import PlaceholderRender, read_guard_map
from renderer.standalone_map import coverage_problems, fleet_render, guard_map


@pytest.fixture(scope="module")
def render():
    return fleet_render()


@pytest.fixture(scope="module")
def known_keys():
    return set(homelab_render.secret_keys())


def test_the_committed_map_covers_every_standalone_use_is_clean(render, known_keys):
    assert coverage_problems(read_guard_map(), render, known_keys) == []


def test_the_named_members_are_where_the_guard_expects_them(render):
    assert "email" in read_guard_map()["traefik"]
    # R2's PR 7b moved mullvad_account into a Secret placeholder mode leaves out, so it
    # stands alone nowhere and the guard needs no exemption for it.
    assert "mullvad_account" not in render.standalone["qbittorrent"]
    assert "wireguard_interface_private_key" in read_guard_map()["qbittorrent"]


def test_a_map_missing_a_standalone_key_is_flagged(render, known_keys):
    committed = read_guard_map()
    committed["traefik"] = [k for k in committed["traefik"] if k != "email"]
    (problem,) = coverage_problems(committed, render, known_keys)
    assert problem.startswith("traefik: ") and "standalone_map.py" in problem


def test_a_mapped_key_absent_from_the_secrets_file_is_flagged(render, known_keys):
    assert coverage_problems(read_guard_map(), render, known_keys - {"email"}) == [
        "email is in the map and not in ansible/vars/secrets.yml"
    ]


def test_a_per_tag_kinds_standalone_keys_count_for_its_tag():
    standalone = {
        "x": frozenset({"a"}),
        "x-deferred": frozenset({"b"}),
        "x-verify": frozenset({"c"}),
    }
    render = PlaceholderRender([], {}, standalone)
    assert guard_map(render) == {"x": ["a", "b", "c"]}
