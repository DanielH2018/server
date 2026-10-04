#!/usr/bin/env python3
"""The nginx alpine digests soak 1 day, the rest of the k8s plane still soaks 3 (#2886).

`nginx:alpine` is re-pushed about every 3.6 days and Renovate always proposes the newest
digest, so a 3-day soak restarted on every re-push left an automerge window the once-daily
Renovate run usually missed — the last two digests on master were landed by hand. The exception
in `renovate.json` lowers the soak for exactly two pins, and only on the digest axis.

Three things can break it silently, so each gets an assertion:

- The exception is scoped by `matchFileNames` and `matchPackageNames`, so moving or renaming
  either pin leaves the rule matching nothing. `NGINX_ALPINE_PINS` is the named-member census
  that fails instead.
- The rule only works where it SITS: after the digest rule it overrides. `_resolve_setting`
  walks the rules in order, the way Renovate does, so a rule hoisted above the digest rule
  fails here rather than reading correct in isolation.
- Scoping it to `digest`/`pinDigest` is what keeps a real `nginxinc/nginx-unprivileged` version
  release on the 7-day soak. A rule that lost `matchUpdateTypes` still reads 1 day for a digest.

Run: uv run pytest scripts/tests/test_renovate_nginx_digest_soak.py
"""

import pytest
from _renovate import _REPO, _resolve_setting

# The two pins the exception covers: package name -> the file carrying it. Named rather than
# derived from the rule, so a renamed var or a moved pin fails as a missing member instead of
# leaving the rule matching nothing (the pattern-census rule in .claude/rules/python-layout.md).
NGINX_ALPINE_PINS = {
    "nginx": "ansible/roles/k8s/freshrss/defaults/main.yml",
    "nginxinc/nginx-unprivileged": "ansible/roles/k8s/docs/defaults/main.yml",
}

# A k8s digest pin outside the exception, to prove the 3-day soak is still the plane's
# default. Its auto-deploy flag is irrelevant here: the denylist rule overrides automerge,
# never minimumReleaseAge.
CONTROL_PIN = (
    "ghcr.io/flaresolverr/flaresolverr",
    "ansible/roles/k8s/prowlarr/defaults/main.yml",
)

NGINX_SOAK = "1 day"
DIGEST_SOAK = "3 days"
VERSION_SOAK = "7 days"


def _soak(dep_name: str, rel_path: str, update_type: str) -> str | None:
    return _resolve_setting(
        "minimumReleaseAge", dep_name, rel_path, update_type, "docker"
    )


@pytest.mark.parametrize("package,rel_path", sorted(NGINX_ALPINE_PINS.items()))
def test_each_nginx_alpine_pin_still_sits_where_the_rule_looks(
    package: str, rel_path: str
) -> None:
    """The pin is an `_image:` line in that file on an alpine tag, or the rule is inert."""
    lines = [
        line
        for line in (_REPO / rel_path).read_text().splitlines()
        if f"_image: {package}:" in line
    ]
    assert lines, (
        f"{rel_path} no longer carries an `_image: {package}:` pin — the #2886 soak exception "
        "in renovate.json matches nothing; move its matchFileNames/matchPackageNames with it"
    )
    tag = lines[0].split(f"_image: {package}:", 1)[1].split("@")[0]
    assert "alpine" in tag, (
        f"{rel_path} pins {package}:{tag}, no longer an alpine tag — the ~3.6-day Alpine "
        "rebuild cycle is the whole argument for the 1-day soak, so re-measure the re-push "
        "cycle of this tag before keeping the rule"
    )


@pytest.mark.parametrize("package,rel_path", sorted(NGINX_ALPINE_PINS.items()))
@pytest.mark.parametrize("update_type", ["digest", "pinDigest"])
def test_a_nginx_alpine_digest_soaks_one_day(
    package: str, rel_path: str, update_type: str
) -> None:
    assert _soak(package, rel_path, update_type) == NGINX_SOAK


def test_a_nginx_unprivileged_version_bump_still_soaks_the_full_seven_days() -> None:
    """1.31 -> 1.32 is an upstream release, not an Alpine rebuild, so it keeps the long soak."""
    package, rel_path = (
        "nginxinc/nginx-unprivileged",
        NGINX_ALPINE_PINS["nginxinc/nginx-unprivileged"],
    )
    assert _soak(package, rel_path, "minor") == VERSION_SOAK
    assert _soak(package, rel_path, "patch") == VERSION_SOAK


def test_a_k8s_digest_outside_the_exception_still_soaks_three_days() -> None:
    package, rel_path = CONTROL_PIN
    assert f"_image: {package}:" in (_REPO / rel_path).read_text(), (
        f"{rel_path} no longer pins {package} — pick another eligible k8s digest pin as the "
        "control, or this test proves nothing about the plane's default soak"
    )
    assert _soak(package, rel_path, "digest") == DIGEST_SOAK


# --- the red-proof pair: the resolver must read the real order, and reject a hoisted rule ---

_DIGEST_RULE = {
    "matchManagers": ["custom.regex"],
    "matchFileNames": ["ansible/roles/k8s/**"],
    "matchUpdateTypes": ["digest", "pinDigest"],
    "minimumReleaseAge": DIGEST_SOAK,
}
_NGINX_RULE = {
    "matchManagers": ["custom.regex"],
    "matchDatasources": ["docker"],
    "matchFileNames": ["ansible/roles/k8s/freshrss/defaults/main.yml"],
    "matchPackageNames": ["nginx"],
    "matchUpdateTypes": ["digest", "pinDigest"],
    "minimumReleaseAge": NGINX_SOAK,
}
_FRESHRSS_DEFAULTS = "ansible/roles/k8s/freshrss/defaults/main.yml"


def test_the_exception_after_the_digest_rule_wins() -> None:
    soak = _resolve_setting(
        "minimumReleaseAge",
        "nginx",
        _FRESHRSS_DEFAULTS,
        "digest",
        "docker",
        [_DIGEST_RULE, _NGINX_RULE],
    )
    assert soak == NGINX_SOAK


def test_the_exception_hoisted_above_the_digest_rule_is_overridden() -> None:
    """The failure the real config's rule ORDER prevents: the 3-day rule wins instead."""
    soak = _resolve_setting(
        "minimumReleaseAge",
        "nginx",
        _FRESHRSS_DEFAULTS,
        "digest",
        "docker",
        [_NGINX_RULE, _DIGEST_RULE],
    )
    assert soak == DIGEST_SOAK
