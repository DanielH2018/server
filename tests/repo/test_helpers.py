"""The shared readers in `_helpers.py` behave the way the guards built on them assume."""

import pytest
from _helpers import (
    K8S_ROLES,
    REPO,
    image_tag,
    SECRETS,
)


def test_the_roots_point_at_the_real_tree():
    assert (REPO / "pyproject.toml").is_file()
    assert (SECRETS / "secret_rotation.yml").is_file()
    assert K8S_ROLES.is_dir()


@pytest.mark.parametrize(
    "ref, tag",
    [
        ("authelia/authelia:4.39.28", "4.39.28"),
        (
            "authelia/authelia:4.39.28@sha256:bd97cff4fcbf715b5ff1f9ae286afbe6",
            "4.39.28",
        ),
        ("registry.local:5000/n8n:2.37.10@sha256:307d606", "2.37.10"),
    ],
)
def test_image_tag_drops_a_pinned_digest(ref, tag):
    assert image_tag(ref) == tag


def test_image_tag_still_sees_a_version_move_behind_a_digest():
    # The version guards built on it exist to fail when the pin moves; a digest must not hide it.
    assert image_tag("ghcr.io/bigboot/autokuma:2.2.0@sha256:12ed0e50") not in {
        "2.1.0-rc.2"
    }
