#!/usr/bin/env python3
"""Extra unattended-upgrades origins must use Origins-Pattern's `key=value` form.

unattended-upgrades accepts two spellings for "which repos may be upgraded automatically",
and one of them fails silently against a repo that publishes no `Suite:` field:

  Unattended-Upgrade::Allowed-Origins::  "gh:stable"                  # legacy
  Unattended-Upgrade::Origins-Pattern::  "origin=gh,codename=stable"  # key=value

`get_allowed_origins_legacy` in /usr/bin/unattended-upgrade rewrites every Allowed-Origins
entry to `o=X,a=Y`, so the second half can only ever match a package file's `archive`. The
GitHub CLI repo has none — measured on daniel-box 2026-08-24 via python-apt, its package file
reads origin='gh', archive='', codename='stable'. `gh:stable` therefore matched nothing and
upgraded nothing, while `apt-config dump` listed it and the drop-in read as correct.

That is the failure this test exists to catch: the legacy form is not wrong at parse time, at
render time, or at deploy time. It is wrong only in what it silently declines to upgrade, which
nothing else in the repo can see. It shipped that way in 2fc0b537.

The distro's own 50unattended-upgrades still uses Allowed-Origins for the security pockets and
is not ours to change; this covers only the extras we add.

Run: uv run pytest ansible/tests/test_unattended_origins_pattern.py
"""

import re
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
GROUP_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"
ACCESS_TASKS = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "access.yml"

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
    value = yaml.safe_load(GROUP_VARS.read_text())[VAR]
    assert isinstance(value, list), f"{VAR} must be a list, got {type(value).__name__}"
    return value


def test_var_is_defined():
    assert VAR in yaml.safe_load(GROUP_VARS.read_text()), (
        f"{VAR} is missing from group_vars/all.yml; access.yml renders it unconditionally "
        f"and a tag-scoped run would die on the undefined var."
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


def test_task_renders_origins_pattern_not_allowed_origins():
    """The drop-in must append via Origins-Pattern, and must not displace the security pockets."""
    body = ACCESS_TASKS.read_text()
    # Comments are stripped first: the task's own comment explains the Allowed-Origins trap by
    # name, and the check below would otherwise fire on the explanation rather than the config.
    body = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'Unattended-Upgrade::Origins-Pattern:: "{{ pattern }}"' in body, (
        "access.yml no longer renders the extras as Origins-Pattern:: entries"
    )
    assert "Unattended-Upgrade::Allowed-Origins" not in body, (
        "access.yml declares Allowed-Origins; the extras belong in Origins-Pattern, and "
        "the security pockets are the distro 50unattended-upgrades' to own"
    )
    assert "Origins-Pattern {" not in body, (
        "a `{ ... }` block reads as a replacement of the distro's list; use the `::` "
        "append syntax"
    )
