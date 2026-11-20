#!/usr/bin/env python3
"""Guard the Renovate manager and rules over the Pi's Compose images (target slice H5, PR H5.11).

The manager reads `image:` lines in hosts/components/pi-compose/services/*/compose.yaml.j2.
Renovate's extract, held against `scripts/validate/renovate_expected_deps.tsv` in CI, proves
it reads every service's image with its digest and that its autoReplaceStringTemplate writes
each live pin back unchanged. The guards here check two things the extract cannot see:

- the table extracts from exactly the four compose files, so a service that loses its manager
  fails as a missing member;
- the pinDigests rule reaches each compose file.

The `pi-` and `alloy-` rows of test_renovate_package_rules.py hold the packageRules to what
the operator ruled on 2026-10-29 (H5's Open question 1).

Run: uv run pytest scripts/tests/test_renovate_pi_compose_pins.py
"""

import pytest

from _renovate import EXPECTED_DEPS, _PACKAGE_RULES
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


@pytest.mark.parametrize("path", sorted(PI_IMAGES))
def test_the_pin_digests_rule_reaches_each_compose_file(path: str) -> None:
    """Without it a version bump carries no newDigest, and the Pi pulls the old digest."""
    assert _pin_digest_rule_for(path) is _PACKAGE_RULES[-1]
