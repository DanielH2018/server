"""The standalone-value guard's committed map covers every standalone use (target slice R2).

The deploy path reads `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`, not the
render, so a template change that adds a standalone use would leave the map short. This test
renders the fleet in placeholder mode once and holds the map to it.

Run: uv run pytest scripts/renderer/tests/test_renderer_standalone_map.py
"""

import pytest

from renderer import homelab_render
from renderer.renderer_lib.placeholders import GUARD_DEFERRED, read_guard_map
from renderer.standalone_map import coverage_problems, fleet_render


@pytest.fixture(scope="module")
def render():
    return fleet_render()


@pytest.fixture(scope="module")
def known_keys():
    return set(homelab_render.secret_keys())


def test_the_committed_map_covers_every_standalone_use_is_clean(render, known_keys):
    assert coverage_problems(read_guard_map(), render, known_keys) == []


def test_the_named_members_are_where_the_guard_expects_them(render):
    assert "domain" in read_guard_map()["homepage"]
    assert "mullvad_account" in GUARD_DEFERRED
    assert "mullvad_account" in render.standalone["qbittorrent"]


def test_a_map_missing_a_standalone_key_is_flagged(render, known_keys):
    committed = read_guard_map()
    committed["homepage"] = [k for k in committed["homepage"] if k != "domain"]
    (problem,) = coverage_problems(committed, render, known_keys)
    assert problem.startswith("homepage: ") and "standalone_map.py" in problem


def test_a_key_both_mapped_and_deferred_is_flagged(render, known_keys):
    deferred = GUARD_DEFERRED | {"domain"}
    committed = read_guard_map()
    assert "domain is both in the map and in GUARD_DEFERRED" in coverage_problems(
        committed, render, known_keys, deferred
    )


def test_a_deferred_key_with_no_standalone_use_is_flagged(render, known_keys):
    deferred = GUARD_DEFERRED | {"no_such_standalone_key"}
    assert coverage_problems(read_guard_map(), render, known_keys, deferred) == [
        "no_such_standalone_key is in GUARD_DEFERRED and stands alone nowhere"
    ]


def test_a_mapped_key_absent_from_the_secrets_file_is_flagged(render, known_keys):
    assert coverage_problems(read_guard_map(), render, known_keys - {"domain"}) == [
        "domain is in the map and not in ansible/vars/secrets.yml"
    ]
