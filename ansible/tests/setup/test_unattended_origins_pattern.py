#!/usr/bin/env python3
"""Extra unattended-upgrades origins must use Origins-Pattern's `key=value` form.

unattended-upgrades accepts two spellings for "which repos may be upgraded automatically",
and one of them fails silently against a repo that publishes no `Suite:` field:

  Unattended-Upgrade::Allowed-Origins::  "gh:stable"                  # legacy
  Unattended-Upgrade::Origins-Pattern::  "origin=gh,codename=stable"  # key=value

`get_allowed_origins_legacy` in /usr/bin/unattended-upgrade rewrites every Allowed-Origins
entry to `o=X,a=Y`, so the second half can only ever match a package file's `archive`. The
GitHub CLI repo has none — measured on daniel-box via python-apt, its package file
reads origin='gh', archive='', codename='stable'. `gh:stable` therefore matched nothing and
upgraded nothing, while `apt-config dump` listed it and the drop-in read as correct.

That is the failure this test exists to catch: the legacy form is not wrong at parse time, at
render time, or at deploy time. It is wrong only in what it silently declines to upgrade, which
nothing else in the repo can see.

The distro's own 50unattended-upgrades still uses Allowed-Origins for the security pockets and
is not ours to change; this covers only the extras we add.

Run: uv run pytest ansible/tests/setup/test_unattended_origins_pattern.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import ALL_VARS
from _host_packages import HOSTS, shipped_text

DROP_IN = "/etc/apt/apt.conf.d/52unattended-upgrades-local"

VAR = "unattended_upgrades_origins_patterns"
# `origin=gh`, `archive=${distro_codename}-updates`, `n=stable` — a matcher, `=`, a value.
TOKEN_RE = re.compile(r"^[a-z]+=[^=,]+$")
# The matcher names unattended-upgrade's match_whitelist_string() accepts. Anything else
# raises UnknownMatcherError at run time, which surfaces only in the daily timer's log.
MATCHERS = {
    "o",
    "origin",
    "l",
    "label",
    "a",
    "suite",
    "archive",
    "c",
    "component",
    "site",
    "n",
    "codename",
}


def patterns() -> list[str]:
    value = yaml_fast.safe_load(ALL_VARS.read_text())[VAR]
    assert isinstance(value, list), f"{VAR} must be a list, got {type(value).__name__}"
    return value


def test_var_is_defined():
    assert VAR in yaml_fast.safe_load(ALL_VARS.read_text()), (
        f"{VAR} is missing from group_vars/all.yml; the unattended-upgrades component renders it unconditionally "
        f"and a tag-scoped run would die on the undefined var."
    )


def matches(pattern: str, release: dict[str, str]) -> bool:
    """Whether every token of `pattern` matches `release`, as match_whitelist_string reads it."""
    filled = pattern.replace("${distro_id}", "Ubuntu").replace(
        "${distro_codename}", "noble"
    )
    return all(
        release.get(key) == value
        for key, value in (token.split("=", 1) for token in filled.split(","))
    )


def test_the_operators_widening_reaches_the_regular_updates_pocket():
    release = {"origin": "Ubuntu", "archive": "noble-updates"}
    assert any(matches(pattern, release) for pattern in patterns())


# The fields `apt-cache policy` prints for these repositories on daniel-pi, 2026-11-07.
@pytest.mark.parametrize(
    "name, release",
    [
        ("Docker", {"origin": "Docker", "archive": "noble", "codename": ""}),
        ("homelab", {"origin": "homelab", "archive": "homelab", "codename": "homelab"}),
    ],
)
def test_no_pattern_reaches_docker_or_the_homelab_repository(name, release):
    # An unattended engine swap took the Pi down (#1961), and the updater alone moves homelab-*.
    assert not any(matches(pattern, release) for pattern in patterns()), name


def test_the_match_reader_can_find_docker_when_a_pattern_names_it():
    assert matches(
        "origin=Docker,archive=noble", {"origin": "Docker", "archive": "noble"}
    )


@pytest.mark.parametrize("pattern", patterns())
def test_pattern_uses_key_value_form(pattern: str):
    """No bare `origin:archive` entries — that is the legacy form that silently under-matches."""
    assert "=" in pattern, (
        f"{pattern!r} looks like the legacy Allowed-Origins form. Use Origins-Pattern's "
        f"key=value form instead, e.g. 'origin=gh,codename=stable' — the legacy form is "
        f"rewritten to o=X,a=Y and cannot match a repo that publishes no Suite: field."
    )


@pytest.mark.parametrize("pattern", patterns())
def test_pattern_tokens_are_well_formed(pattern: str):
    for token in pattern.split(","):
        assert TOKEN_RE.match(token), f"malformed token {token!r} in {pattern!r}"
        matcher = token.split("=", 1)[0]
        assert matcher in MATCHERS, (
            f"unknown matcher {matcher!r} in {pattern!r}; unattended-upgrade raises "
            f"UnknownMatcherError on anything outside {sorted(MATCHERS)}"
        )


@pytest.mark.parametrize("host", HOSTS)
def test_package_ships_origins_pattern_not_allowed_origins(host: str):
    """The drop-in must append via Origins-Pattern, and must not displace the security pockets."""
    body = shipped_text(host, DROP_IN)
    for pattern in patterns():
        assert f'Unattended-Upgrade::Origins-Pattern:: "{pattern}";' in body, (
            f"{host}'s drop-in no longer carries {pattern!r} as an Origins-Pattern:: entry"
        )
    assert "Unattended-Upgrade::Allowed-Origins" not in body, (
        f"{host}'s drop-in declares Allowed-Origins; the extras belong in Origins-Pattern, "
        "and the security pockets are the distro 50unattended-upgrades' to own"
    )
    assert "Origins-Pattern {" not in body, (
        "a `{ ... }` block reads as a replacement of the distro's list; use the `::` "
        "append syntax"
    )
