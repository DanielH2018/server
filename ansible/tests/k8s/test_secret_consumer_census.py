#!/usr/bin/env python3
"""A rotation is only finished when every consumer holds the new value.

`secret_rotation.py audit` answers "what is due". Until 2026-08-29 nothing answered "who is
still holding the old one", and that gap cost a real outage: the Sonarr and Radarr API keys
were rotated, six of seven consumers were missed, and exportarr 401'd for ~40 minutes across
seven Kuma monitors before anyone noticed. The seventh consumer, `setup/fake_remux`, is the
one `deploy.sh` structurally cannot reach.

`tree_consumers()` measures the answer from the tree. These tests pin the two invariants that
make it trustworthy, each with the input it must accept AND the input it must reject — a
census that finds everything and one that finds nothing look identical from the passing side.

Run: uv run pytest ansible/tests/k8s/test_secret_consumer_census.py
"""

import sys as _sys

import pytest
from _helpers import REPO

_REPO = REPO
_sys.path.insert(0, str(_REPO / "scripts" / "secrets_mgmt"))

import secret_rotation as sr  # noqa: E402 — needs the path insert above


# The incident this file exists for, kept as the accept case. Verified by hand on 2026-08-29
# against the live cluster: each of these renders or reads sonarr_api_key.
SONARR_KEY_CONSUMERS = {
    "autofix-bridge": "deploy",
    "configarr": "deploy",
    "fake_remux": "setup",
    "homepage": "deploy",
    "janitorr": "deploy",
    "monitor-bridge": "deploy",
    "sonarr": "deploy",
}


def _phantom_tags(name: str, tags) -> list[str]:
    """Tags claiming a role that does not reference the secret.

    This is the 2026-08-25 defect (review M-8b) in executable form: nine of 41
    `monitor_bridge_*` tokens routed by name prefix to a role that renders them NOWHERE, so
    `rotate --deploy` wrote a new value, deployed the wrong role, left the real pusher on the
    old token and stamped `last_rotated` green. The fix was a hand-run `grep -rl`; this makes
    that grep repeatable.
    """
    census = sr.tree_consumers(name)
    return [tag for tag in tags if tag not in census]


def _setup_plane_blind_spots(name: str, tags) -> list[str]:
    """Setup-plane consumers of a secret that `rotate --deploy` believes it can finish.

    A non-empty `consumer_tags()` means the unattended path will rotate, deploy those tags and
    record success. `deploy.sh` derives valid tags from `containers_list`, so a setup-plane
    role is not among them — the rotation would stamp green with that consumer still holding
    the old value. Empty tags are fine here: those secrets are MANUAL by design and the audit
    keeps reminding.
    """
    if not tags:
        return []
    return sorted(
        role for role, plane in sr.tree_consumers(name).items() if plane == "setup"
    )


def test_the_census_finds_every_known_consumer_of_the_secret_that_caused_the_incident():
    assert sr.tree_consumers("sonarr_api_key") == SONARR_KEY_CONSUMERS


def test_the_census_reports_nothing_for_a_name_no_role_references():
    """The control. Without it, a census that matched everything would pass the test above."""
    assert sr.tree_consumers("sonarr_api_key_that_does_not_exist_anywhere") == {}


def test_the_census_separates_the_plane_deploy_sh_cannot_reach():
    consumers = sr.tree_consumers("sonarr_api_key")

    assert consumers["fake_remux"] == "setup", (
        "fake_remux is the consumer deploy.sh cannot reach; if it stops being classified as "
        "setup-plane, the census stops warning about the exact trap it was written for"
    )
    assert consumers["sonarr"] == "deploy"


def test_the_repair_commands_route_each_plane_to_a_playbook_that_can_reach_it():
    commands = sr.consumer_commands("sonarr_api_key")

    deploy_cmd = [c for c in commands if "deploy.sh" in c]
    setup_cmd = [c for c in commands if "initial_setup.yml" in c]

    assert len(deploy_cmd) == 1 and len(setup_cmd) == 1
    assert "fake_remux" not in deploy_cmd[0], (
        "deploy.sh exits 2 on a tag that is not in containers_list, having deployed NOTHING — "
        "putting a setup-plane role in this command is the silent half of the bug"
    )
    assert "--tags fake_remux" in setup_cmd[0]


@pytest.mark.parametrize("name", sr.secret_names())
def test_no_declared_consumer_tag_names_a_role_that_never_references_the_secret(name):
    """Every tag consumer_tags() routes to must actually render the secret."""
    phantom = _phantom_tags(name, sr.consumer_tags(name))

    assert not phantom, (
        f"{name} routes to {phantom}, which reference it nowhere in ansible/roles/. "
        "`rotate --deploy` would deploy that role, leave the real consumer on the old value, "
        "and stamp last_rotated green."
    )


@pytest.mark.parametrize("name", sr.secret_names())
def test_no_auto_deployable_secret_hides_a_setup_plane_consumer(name):
    blind = _setup_plane_blind_spots(name, sr.consumer_tags(name))

    assert not blind, (
        f"{name} is treated as auto-deployable but is also consumed by setup-plane role(s) "
        f"{blind}, which deploy.sh cannot reach. The rotation would report success with that "
        "consumer still holding the old value."
    )


def test_the_phantom_check_goes_red_on_a_tag_that_references_nothing():
    """The reject half of the guard above — proof it can fail.

    Without this, a `_phantom_tags` that had quietly stopped matching would keep the suite
    green forever, which is precisely how the original mis-routing survived 41 tokens.
    """
    phantom = _phantom_tags("sonarr_api_key", ("sonarr", "a-role-that-does-not-exist"))

    assert phantom == ["a-role-that-does-not-exist"]


def test_the_setup_plane_check_goes_red_when_a_setup_consumer_is_claimed_deployable():
    """The reject half: a setup-plane consumer claimed auto-deployable must be flagged.

    sonarr_api_key genuinely has a setup-plane consumer, so claiming it is auto-deployable must be
    flagged. This is today's incident, replayed.
    """
    blind = _setup_plane_blind_spots("sonarr_api_key", ("sonarr",))

    assert blind == ["fake_remux"]
