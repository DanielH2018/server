#!/usr/bin/env python3
"""A channel-tag-plus-digest `_image:` pin whose regression is expensive stays off auto-deploy.

Issue #1524. A pin of the shape `<image>:latest@sha256:<64 hex>` moves only its digest when
Renovate bumps it: the tag text never changes, so the diff carries no version to compare and a
digest resolving to an OLDER release is indistinguishable from one resolving to a newer one.
PR #1440 proposed exactly that downgrade on n8n through nine green checks (issue #1493).

n8n's group is `automerge: false` and a human reads the PR. On `roles/k8s/*/defaults/main.yml`
the same shape is automerged (renovate.json's digest rule) AND auto-deployable, so there is no
human in it at all.

# DECIDED: the class is accepted for the services whose regression is cheap, and fenced by the
# auto-deploy denylist for the ones whose regression is not. Censused 2026-09-10: of the 13 k8s
# roles carrying this pin shape, the three #1524 named as expensive — qbittorrent, tdarr,
# healthchecks — were ALREADY denied auto-deploy, as were cloudflare-ddns and dri-device-plugin.
# The eight that remain eligible are stateless front-ends and probes on `:latest`, where a
# backwards step costs a redeploy and no data. Option 3 in the issue (a CI job resolving each
# digest to a version) buys a version comparison for those eight at the price of a network
# transport nothing else in CI needs; option 1's ledger costs a manual append per bump on
# sixteen automerged pins. Neither is worth it at that exposure. What this file makes
# executable is the FENCE rather than the acceptance: the expensive three may not quietly
# become auto-deployable while still carrying an unreadable pin.

Run: uv run pytest ansible/tests/deploy/test_channel_tag_digest_pins_stay_off_autodeploy.py
"""

import re

import pytest

from _autodeploy import _denylist, _roles

# `<anything>:<channel tag>@sha256:<digest>` on an `_image:` var. The channel tags are the
# mutable ones: a bump against them can only ever be a digest, never a version.
_PIN = re.compile(
    r"^\s*\w+_image:\s*[\"']?[^:\"'\s]+:(?:stable|latest|main|edge|nightly|develop)@sha256:[0-9a-f]{64}",
    re.MULTILINE,
)

# The roles #1524 named as the expensive subset: a torrent client mid-transfer, a transcoder
# holding a queue, and the service every OTHER check reports through. Named rather than derived
# — "expensive" is a judgement about what a regression costs, which no file states.
_EXPENSIVE = frozenset({"qbittorrent", "tdarr", "healthchecks"})

# The floor the census must clear. Below it the regex has stopped matching and every assertion
# built on it passes over an empty set (CLAUDE.md: a check that finds its own subject by
# pattern ships with a named member it must find).
_CENSUS_FLOOR = 10


def _pinned_roles() -> set[str]:
    """Every k8s role whose defaults carry a channel-tag-plus-digest `_image:` pin."""
    found = set()
    for role in _roles():
        defaults = role / "defaults/main.yml"
        if defaults.is_file() and _PIN.search(defaults.read_text()):
            found.add(role.name)
    return found


def test_the_census_still_finds_the_pins_it_is_about():
    """Non-vacuity, both ways: the shape is still present, and still on the named roles."""
    pinned = _pinned_roles()
    assert len(pinned) >= _CENSUS_FLOOR, sorted(pinned)
    assert _EXPENSIVE <= pinned, sorted(_EXPENSIVE - pinned)


def test_a_version_pin_is_not_read_as_a_channel_pin():
    """The must-not-fire half: this guard is about pins with NO version in them."""
    assert not _PIN.search(
        "  sonarr_k8s_image: lscr.io/sonarr:4.0.15@sha256:" + "a" * 64
    )
    assert not _PIN.search("  sonarr_k8s_image: lscr.io/sonarr:latest\n")


@pytest.mark.parametrize("role", sorted(_EXPENSIVE))
def test_an_expensive_channel_pin_is_denied_auto_deploy(role):
    """The RED half: promote one of these while its pin stays unreadable and this fails.

    A digest bump on an auto-deployable role merges and deploys itself, so for these three the
    first sign of a downgrade would be the service behaving like an older release. Either keep
    the role denylisted, or move its pin to a version tag a diff can be read against — the
    guard accepts the second remedy because the pin then leaves the census.
    """
    if role not in _pinned_roles():
        pytest.skip(f"{role} no longer carries a channel-tag-plus-digest pin")
    assert role in _denylist(), (
        f"{role} carries a `:<channel>@sha256:` pin, whose bumps automerge with no version in "
        "the diff, and is no longer denied auto-deploy — see the DECIDED marker in this file"
    )
