"""A PR's kind and the quota floor (`land_lib/live_limit.py`).

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_live_limit.py
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from deploy_tools.land_lib import live_limit
from lib.repo_paths import REPO

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
K8S_PATH = "k8s/roles/sonarr/templates/deployment.yaml.j2"

QUIET = live_limit.Reads(
    core_quota=lambda: (5000, int(NOW.timestamp()) + 3600),
    now=lambda: NOW,
)


def test_a_low_quota_far_from_its_reset_lowers_the_limit_to_one():
    reset = int(NOW.timestamp()) + 40 * 60
    why = live_limit.low_quota(replace(QUIET, core_quota=lambda: (1234, reset)))
    assert why == "the core quota has 1234 calls left and resets in 40m"


def test_a_low_quota_about_to_reset_lowers_nothing():
    reset = int(NOW.timestamp()) + 10 * 60
    assert (
        live_limit.low_quota(replace(QUIET, core_quota=lambda: (1234, reset))) is None
    )


def test_a_failed_read_lowers_nothing():
    def refused():
        raise OSError("gh: API rate limit exceeded")

    assert live_limit.low_quota(replace(QUIET, core_quota=refused)) is None


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("k8s/roles/longhorn/defaults/main.yml", live_limit.SERIAL),
        ("k8s/roles/metallb/defaults/main.yml", live_limit.SERIAL),
        ("k8s/roles/flux/templates/sync.yaml.j2", live_limit.SERIAL),
        ("secrets/cluster-vars.sops.env", live_limit.SERIAL),
        ("k8s/services.yml", live_limit.SERIAL),
        (K8S_PATH, live_limit.K8S),
        ("k8s/roles/sonarr/defaults/main.yml", live_limit.K8S),
        ("docs/landing.md", ""),
    ],
)
def test_a_prs_paths_decide_its_kind(path, kind):
    assert live_limit.kind([path]) == kind


def test_every_serial_role_is_a_k8s_role_directory():
    """`SERIAL_ROLES` is derived from pin names, so a pin moved to another role must fail here."""
    assert {"longhorn", "metallb", "flux"} <= live_limit.SERIAL_ROLES
    for role in live_limit.SERIAL_ROLES:
        assert (REPO / "k8s" / "roles" / role).is_dir(), role


def test_every_serial_path_is_a_file():
    """A renamed service list matches no PR, so the rule would bar nothing."""
    assert "k8s/services.yml" in live_limit.SERIAL_PATHS
    for path in live_limit.SERIAL_PATHS:
        assert (REPO / path).is_file(), path
