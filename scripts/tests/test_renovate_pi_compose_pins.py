#!/usr/bin/env python3
"""Guard the Renovate manager and rules over the Pi's Compose images (target slice H5, PR H5.11).

The manager reads `image:` lines in hosts/components/pi-compose/services/*/compose.yaml.j2.
Renovate's extract, held against `scripts/validate/renovate_expected_deps.tsv` in CI, proves
it reads every service's image with its digest and that its autoReplaceStringTemplate writes
each live pin back unchanged. The guards here check two things the extract cannot see:

- the table extracts from exactly the four compose files, so a service that loses its manager
  fails as a missing member;
- the packageRules resolve as the operator ruled on 2026-10-29 (H5's Open question 1).
  Digest bumps to autoheal and docker-proxy automerge. wg-easy stays manual. The two alloy
  pins share one manual group.

Run: uv run pytest scripts/tests/test_renovate_pi_compose_pins.py
"""

import pytest

from _renovate import EXPECTED_DEPS, _PACKAGE_RULES, _resolve_setting
from test_renovate_managers import _pin_digest_rule_for

_SERVICES = "hosts/components/pi-compose/services"

# Named rather than derived, so a service that loses its manager fails as a missing member
# instead of shrinking the census.
PI_IMAGES = {
    f"{_SERVICES}/alloy/compose.yaml.j2": {"grafana/alloy"},
    f"{_SERVICES}/autoheal/compose.yaml.j2": {"willfarrell/autoheal"},
    f"{_SERVICES}/docker-proxy/compose.yaml.j2": {"lscr.io/linuxserver/socket-proxy"},
    f"{_SERVICES}/wg-easy/compose.yaml.j2": {"ghcr.io/wg-easy/wg-easy"},
}

LOKI_HOMELAB_DEFAULTS = "k8s/roles/loki-homelab/defaults/main.yml"


def test_the_table_extracts_each_compose_image_with_its_digest() -> None:
    extracted: dict[str, set[str]] = {}
    for d in EXPECTED_DEPS:
        if d.file.startswith(f"{_SERVICES}/"):
            assert (d.manager, d.datasource, d.digest) == (
                "regex",
                "docker",
                "digest",
            ), d
            extracted.setdefault(d.file, set()).add(d.dep_name)
    assert extracted == PI_IMAGES


def _resolve(field: str, image: str, path: str, update_type: str, rules=_PACKAGE_RULES):
    return _resolve_setting(field, image, path, update_type, "docker", rules=rules)


_AUTOHEAL = ("willfarrell/autoheal", f"{_SERVICES}/autoheal/compose.yaml.j2")
_DOCKER_PROXY = (
    "lscr.io/linuxserver/socket-proxy",
    f"{_SERVICES}/docker-proxy/compose.yaml.j2",
)
_WG_EASY = ("ghcr.io/wg-easy/wg-easy", f"{_SERVICES}/wg-easy/compose.yaml.j2")
_ALLOY = ("grafana/alloy", f"{_SERVICES}/alloy/compose.yaml.j2")


@pytest.mark.parametrize("pin", [_AUTOHEAL, _DOCKER_PROXY], ids=lambda p: p[0])
def test_a_digest_bump_to_autoheal_or_docker_proxy_automerges(pin) -> None:
    assert _resolve("automerge", *pin, "digest") is True
    assert _resolve("groupName", *pin, "digest") == "pi image {{depName}}"
    # Neither registry returns a timestamp for a `latest` digest.
    assert (
        _resolve("minimumReleaseAgeBehaviour", *pin, "digest") == "timestamp-optional"
    )
    assert _resolve("rebaseWhen", *pin, "digest") == "conflicted"


@pytest.mark.parametrize("update_type", ["digest", "major"])
def test_wg_easy_stays_manual(update_type: str) -> None:
    assert _resolve("automerge", *_WG_EASY, update_type) is False
    assert "(manual" in _resolve("groupName", *_WG_EASY, update_type)


@pytest.mark.parametrize("update_type", ["digest", "minor", "major"])
def test_the_two_alloy_pins_share_one_manual_group(update_type: str) -> None:
    pi_group = _resolve("groupName", *_ALLOY, update_type)
    cluster_group = _resolve(
        "groupName", "grafana/alloy", LOKI_HOMELAB_DEFAULTS, update_type
    )
    assert pi_group == cluster_group
    assert "(manual" in pi_group and "k8s_autodeploy: false" in pi_group
    assert _resolve("automerge", *_ALLOY, update_type) is False


def test_without_the_pi_rules_alloy_falls_into_the_automerging_catch_all() -> None:
    """Proof the resolution guards can go red: the Pi rules are what hold these pins back."""
    rules = [
        r
        for r in _PACKAGE_RULES
        if not any("pi-compose" in g for g in r.get("matchFileNames", []))
        or r.get("pinDigests")
    ]
    assert _resolve("automerge", *_ALLOY, "minor", rules=rules) is True
    assert _resolve("groupName", *_ALLOY, "minor", rules=rules) == (
        "container images (non-major)"
    )


@pytest.mark.parametrize("path", sorted(PI_IMAGES))
def test_the_pin_digests_rule_reaches_each_compose_file(path: str) -> None:
    """Without it a version bump carries no newDigest, and the Pi pulls the old digest."""
    assert _pin_digest_rule_for(path) is _PACKAGE_RULES[-1]
