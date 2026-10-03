#!/usr/bin/env python3
"""A k8s digest with no release timestamp raises instead of pending forever (#3368).

Renovate ages a digest update against the release timestamp of the newest version matching its
tag, and only Docker Hub returns one. A GHCR or lscr.io digest, or any digest on a tag that
names no version (`latest`, `jvm-stable`), has none. Under the default
`minimumReleaseAgeBehaviour: timestamp-required` such an update sits in Pending Status Checks
indefinitely, which is how four digests sat there 4 to 13 days past their 3-day soak. The k8s
digest rule in `renovate.json` sets `timestamp-optional`, so Renovate raises them with no age
check while still enforcing the soak wherever a timestamp exists.

Two things can break it silently, so each gets an assertion:

- A later rule that matches these pins and sets `timestamp-required` again. `_resolve_setting`
  walks the rules in order, so the resolved value, not the digest rule's own body, is asserted.
- The key spreading to version bumps, where it would drop the 7-day soak from real upstream
  releases on GHCR. That is a separate decision, so a `minor`/`patch` must still resolve unset.

Run: uv run pytest scripts/tests/test_renovate_digest_soak_without_timestamp.py
"""

import pytest
from _renovate import _REPO, _resolve_setting

# The digests #3368 found held, as package -> the file carrying the pin. Named rather than
# derived, so a moved pin fails as a missing member instead of resolving against a path no
# rule was written for. cloudflare-ddns is a Docker Hub image on `latest`: it has no
# timestamp because its tag names no version, not because of its registry.
HELD_PINS = {
    "lscr.io/linuxserver/healthchecks": "ansible/roles/k8s/healthchecks/defaults/main.yml",
    "lscr.io/linuxserver/qbittorrent": "ansible/roles/k8s/qbittorrent/defaults/main.yml",
    "ghcr.io/schaka/janitorr": "ansible/roles/k8s/janitorr/defaults/main.yml",
    "favonia/cloudflare-ddns": "ansible/roles/k8s/cloudflare-ddns/defaults/main.yml",
}

OPTIONAL = "timestamp-optional"


def _behaviour(
    dep_name: str, rel_path: str, update_type: str, rules=None
) -> str | None:
    kwargs = {} if rules is None else {"rules": rules}
    return _resolve_setting(
        "minimumReleaseAgeBehaviour",
        dep_name,
        rel_path,
        update_type,
        "docker",
        **kwargs,
    )


@pytest.mark.parametrize("package,rel_path", sorted(HELD_PINS.items()))
def test_each_held_pin_still_sits_where_the_rule_looks(
    package: str, rel_path: str
) -> None:
    assert f"_image: {package}:" in (_REPO / rel_path).read_text(), (
        f"{rel_path} no longer pins {package} — pick another digest pin with no release "
        "timestamp, or this test proves nothing about the k8s digest rule"
    )


@pytest.mark.parametrize("package,rel_path", sorted(HELD_PINS.items()))
@pytest.mark.parametrize("update_type", ["digest", "pinDigest"])
def test_a_digest_with_no_timestamp_is_raised_without_an_age_check(
    package: str, rel_path: str, update_type: str
) -> None:
    assert _behaviour(package, rel_path, update_type) == OPTIONAL


@pytest.mark.parametrize("update_type", ["minor", "patch"])
def test_a_version_bump_still_requires_a_timestamp(update_type: str) -> None:
    package, rel_path = (
        "lscr.io/linuxserver/healthchecks",
        HELD_PINS["lscr.io/linuxserver/healthchecks"],
    )
    assert _behaviour(package, rel_path, update_type) is None


# --- the red-proof pair: the resolver reports the key only where a matching rule sets it ---

_DIGEST_RULE = {
    "matchManagers": ["custom.regex"],
    "matchFileNames": ["ansible/roles/k8s/**"],
    "matchUpdateTypes": ["digest", "pinDigest"],
    "minimumReleaseAge": "3 days",
}
_HEALTHCHECKS = (
    "lscr.io/linuxserver/healthchecks",
    HELD_PINS["lscr.io/linuxserver/healthchecks"],
)


def test_a_digest_rule_setting_the_key_is_clean() -> None:
    rule = {**_DIGEST_RULE, "minimumReleaseAgeBehaviour": OPTIONAL}
    assert _behaviour(*_HEALTHCHECKS, "digest", rules=[rule]) == OPTIONAL


def test_a_digest_rule_without_the_key_is_flagged() -> None:
    """The pre-#3368 config: Renovate's default `timestamp-required` holds the digest."""
    assert _behaviour(*_HEALTHCHECKS, "digest", rules=[_DIGEST_RULE]) is None
