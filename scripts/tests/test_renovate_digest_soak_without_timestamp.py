#!/usr/bin/env python3
"""A k8s image update with no release timestamp raises instead of pending forever.

Renovate ages a digest update against the release timestamp of the newest version matching its
tag, and only Docker Hub returns one. A GHCR or lscr.io digest, or any digest on a tag that
names no version (`latest`, `jvm-stable`), has none. Under the default
`minimumReleaseAgeBehaviour: timestamp-required` such an update sits in Pending Status Checks
indefinitely, which is how four digests sat there 4 to 13 days past their 3-day soak (#3368).
The k8s digest rule in `renovate.json` sets `timestamp-optional`, so Renovate raises them with
no age check while still enforcing the soak wherever a timestamp exists.

A version bump on GHCR or lscr.io has no timestamp either (#3370). bazarr v1.6.2-ls366,
released 2026-09-26, still sat in Pending Status Checks past its 7-day soak. The k8s version
rule sets `timestamp-optional` for `docker` minor and patch bumps, so a Docker Hub release keeps
its 7-day soak and a GHCR or lscr.io release raises on the first run.

Three things can break it silently, so each gets an assertion:

- A later rule that matches these pins and sets `timestamp-required` again. `_resolve_setting`
  walks the rules in order, so the resolved value, not the rule's own body, is asserted.
- The key spreading past the k8s plane. Only there do the autodeploy denylist and the
  deployer's health gate stand behind an update that skips its soak, so a pin outside it must
  still resolve unset.
- The key spreading past the `docker` datasource. A github-releases or pypi pin in a k8s role
  has a timestamp, but nothing here has decided its soak may lapse when one is missing.

Run: uv run pytest scripts/tests/test_renovate_digest_soak_without_timestamp.py
"""

import pytest
from _renovate import _REPO, _resolve_setting

# The digests #3368 found held, as package -> the file carrying the pin. Named rather than
# derived, so a moved pin fails as a missing member instead of resolving against a path no
# rule was written for. cloudflare-ddns is a Docker Hub image on `latest`: it has no
# timestamp because its tag names no version, not because of its registry.
HELD_PINS = {
    "lscr.io/linuxserver/qbittorrent": "ansible/roles/k8s/qbittorrent/defaults/main.yml",
    "ghcr.io/schaka/janitorr": "ansible/roles/k8s/janitorr/defaults/main.yml",
    "favonia/cloudflare-ddns": "ansible/roles/k8s/cloudflare-ddns/defaults/main.yml",
}

# The version bumps #3370 found held, as package -> the file carrying the pin. Named for the
# same reason as HELD_PINS. crane is on gcr.io, the third registry with no timestamps.
HELD_VERSION_PINS = {
    "lscr.io/linuxserver/bazarr": "ansible/roles/k8s/bazarr/defaults/main.yml",
    "lscr.io/linuxserver/sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml",
    "lscr.io/linuxserver/homeassistant": "ansible/roles/k8s/home-assistant/defaults/main.yml",
    "ghcr.io/raydak-labs/configarr": "ansible/roles/k8s/configarr/defaults/main.yml",
    "gcr.io/go-containerregistry/crane": "ansible/roles/k8s/registry/defaults/main.yml",
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


@pytest.mark.parametrize(
    "package,rel_path", sorted({**HELD_PINS, **HELD_VERSION_PINS}.items())
)
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


@pytest.mark.parametrize("package,rel_path", sorted(HELD_VERSION_PINS.items()))
@pytest.mark.parametrize("update_type", ["minor", "patch"])
def test_a_version_bump_with_no_timestamp_is_raised_without_an_age_check(
    package: str, rel_path: str, update_type: str
) -> None:
    assert _behaviour(package, rel_path, update_type) == OPTIONAL


def test_a_version_bump_outside_the_k8s_plane_still_requires_a_timestamp() -> None:
    assert _behaviour("vale-cli/vale", ".github/workflows/ci.yml", "minor") is None


def test_a_non_docker_version_bump_in_a_k8s_role_still_requires_a_timestamp() -> None:
    assert (
        _resolve_setting(
            "minimumReleaseAgeBehaviour",
            "ruff",
            "ansible/roles/k8s/code-server/defaults/main.yml",
            "minor",
            "pypi",
        )
        is None
    )


# --- the red-proof pair: the resolver reports the key only where a matching rule sets it ---

_DIGEST_RULE = {
    "matchManagers": ["custom.regex"],
    "matchFileNames": ["ansible/roles/k8s/**"],
    "matchUpdateTypes": ["digest", "pinDigest"],
    "minimumReleaseAge": "3 days",
}
_QBITTORRENT = (
    "lscr.io/linuxserver/qbittorrent",
    HELD_PINS["lscr.io/linuxserver/qbittorrent"],
)


def test_a_digest_rule_setting_the_key_is_clean() -> None:
    rule = {**_DIGEST_RULE, "minimumReleaseAgeBehaviour": OPTIONAL}
    assert _behaviour(*_QBITTORRENT, "digest", rules=[rule]) == OPTIONAL


def test_a_digest_rule_without_the_key_is_flagged() -> None:
    """The pre-#3368 config: Renovate's default `timestamp-required` holds the digest."""
    assert _behaviour(*_QBITTORRENT, "digest", rules=[_DIGEST_RULE]) is None
