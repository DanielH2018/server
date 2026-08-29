#!/usr/bin/env python3
"""The Pi's publishing-container set is derived, and the derivation must not narrow.

monitor-bridge's detached-container arm compares each Pi container's live published ports
against a set rendered from daniel-pi's `containers_list` — every entry with a `port`. The
derivation is what keeps the set from drifting as containers come and go, but a derivation
can also silently drop a name it was written to cover, which is the failure this pins:
`selectattr('port', 'defined')` returning four names instead of five reads as a narrowing
nobody sees, and the arm goes blind to whichever container fell out.

So this asserts the derivation against the names it was written for on 2026-08-27, and
asserts that the three known non-publishers stay out of it. A container legitimately added
to or removed from the Pi changes the expected set here in the same commit that changes the
inventory — that edit is the review point, which is exactly what an unpinned derivation
does not give you.

Run: uv run pytest ansible/tests/test_pi_publishing_containers.py
"""

import yaml
from _helpers import ANSIBLE


PI_HOST_VARS = ANSIBLE / "inventory" / "host_vars" / "daniel-pi.yml"
ENV_SECRET = (
    ANSIBLE / "roles" / "k8s" / "monitor-bridge" / "templates" / "env-secret.yaml.j2"
)

# Measured against the live glances payload, 2026-08-27: these report a `->` mapping.
# dozzle was the fifth until it was retired 2026-08-29 (see daniel-pi.yml).
EXPECTED_PUBLISHERS = {"glances", "node-exporter", "promtail", "wg-easy"}
# These report `ports: ""` permanently. A rule that flagged them would page forever.
EXPECTED_NON_PUBLISHERS = {"docker-proxy", "autoheal", "docker-proxy-lifecycle"}


def _pi_entries():
    return (yaml.safe_load(PI_HOST_VARS.read_text()) or {}).get("containers_list") or []


def _derived():
    """Mirror the Jinja: containers_list | selectattr('port', 'defined') | map('name')."""
    return {e["name"] for e in _pi_entries() if "port" in e and e.get("name")}


def test_derivation_covers_every_known_publisher():
    missing = EXPECTED_PUBLISHERS - _derived()
    assert not missing, (
        "the derivation dropped %s — monitor-bridge's detached-container arm no longer "
        "watches it" % sorted(missing)
    )


def test_derivation_excludes_the_known_non_publishers():
    # docker-proxy-lifecycle is hardcoded in the Pi's compose and is not in containers_list
    # at all, so it falls out for a second reason; the other two are here by having no port.
    assert not (EXPECTED_NON_PUBLISHERS & _derived())


def test_derivation_has_not_widened_unreviewed():
    extra = _derived() - EXPECTED_PUBLISHERS
    assert not extra, (
        "new Pi container(s) %s publish a port — confirm they really do, then add them to "
        "EXPECTED_PUBLISHERS" % sorted(extra)
    )


def test_env_secret_renders_the_derivation_and_not_a_list():
    # The red-proof half: replacing the Jinja with a hand-typed list would pass every
    # assertion above while reintroducing exactly the drift they exist to prevent.
    text = ENV_SECRET.read_text()
    assert (
        "hostvars['daniel-pi'].containers_list | selectattr('port', 'defined')" in text
    )
    line = next(
        line
        for line in text.splitlines()
        if line.strip().startswith("PI_PUBLISHED_PORTS:")
    )
    assert "pi_publishers" in line, "the value must come from the derived list"
    for name in EXPECTED_PUBLISHERS:
        assert name not in line, "the set is hardcoded, not derived"
