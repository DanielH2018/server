#!/usr/bin/env python3
"""A versioned host-script release must not contain a script that renders a secret inline.

WHY. release_bin.yml keeps the last five releases of a group on disk so a rollback is one
symlink. That retention is the whole point, and it is also why a secret-rendering script cannot
go in one: five releases of a script with an inline token means five copies of a rotated token
still readable on disk, which turns a rollback convenience into a credential archive. The repo
has paid for the general class three times, every occurrence ending in a rotation
(`grep-on-a-script-prints-the-secret-it-embeds`).

Seven host scripts render a credential inline today and carry `no_log: true` where they are
deployed — the two secret-rotation wrappers, claude-otel-health, registry-gc, janitorr-health,
configarr-health and qbittorrent-prefs-check. None of them belongs in a group. Scripts that
SOURCE a token from a separate env file (the kuma-push pattern) hold no secret themselves and
are what these groups are for.

WHY THE REGISTRY IS THE NAME SOURCE. `ansible/secret_rotation.yml` is plaintext by design —
names, dates and tiers, never values — so this guard needs no SOPS key and runs on a CI runner.
Reading `vars/secrets.yml` would need a decrypt, and a guard that cannot run in CI is a guard
that runs nowhere.

Run: uv run pytest ansible/tests/setup/test_release_bin_groups_have_no_secrets.py
"""

import re
import sys
from pathlib import Path

import pytest
import yaml
from _helpers import ANSIBLE

REPO = ANSIBLE.parent

# The resolver is shared with scripts/validate/shell_templates.py so the two checks
# cannot disagree about what a group contains. pytest's `pythonpath` covers the repo root only.
sys.path.insert(0, str(REPO / "scripts"))

from lib import release_bin_groups  # noqa: E402

REGISTRY = ANSIBLE / "secret_rotation.yml"

# Names that appear in the registry but are too generic to match on: a substring hit would flag
# every script mentioning the word. This is the escape hatch for that, so a generic name is
# handled here rather than by weakening the regex.
#
# DECIDED: `domain` is exempt. It is in the registry at `tier: ignore`
# (ansible/secret_rotation.yml:108-110) because it is a hostname, not a credential — leaking it
# costs nothing, and every script that builds a URL names it. Left in, it flags 6 of the 9
# candidate scripts plus one already converted, so the guard would refuse every group and be
# turned off rather than obeyed. Exempting the name is right; loosening the whole-word regex
# that finds it would not be.
GENERIC_NAMES = frozenset({"domain"})


def secret_names():
    data = yaml.safe_load(REGISTRY.read_text()) or {}
    return sorted(set(data.get("secrets", {})) - GENERIC_NAMES)


def scan_for_secrets(text, names):
    """Return the secret names `text` references, as whole words.

    Whole-word, not substring: `authelia_secret` must not be found inside a comment about
    `authelia_secret_rotation`, and more importantly a short name must not match a longer
    unrelated identifier that happens to contain it.
    """
    return sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", text))


def release_bin_sources():
    """Every `src` handed to release_bin.yml anywhere in the tree, as (task_file, src).

    Delegates to the shared resolver so this guard and `validate/shell_templates.py` cannot
    disagree about a group's contents. This function used to keep its own copy, which iterated
    `release_bin_templates` looking for dicts — and that key is a folded Jinja string naming a
    group, so it matched nothing and the guard passed having scanned zero files.
    `test_discovery_finds_the_converted_group` is the proof that it scans something.
    """
    return [
        (path.relative_to(REPO), src)
        for path, src in release_bin_groups.iter_sources(ANSIBLE / "roles")
    ]


def test_no_versioned_host_script_renders_a_secret():
    names = secret_names()
    offenders = []
    for task_file, src in release_bin_sources():
        source = REPO / src
        if not source.is_file():
            offenders.append(f"{task_file}: {src} does not exist")
            continue
        hits = scan_for_secrets(source.read_text(), names)
        if hits:
            offenders.append(f"{task_file}: {src} renders {', '.join(hits)}")
    assert not offenders, (
        "A versioned release keeps five copies on disk, so a script that renders a secret "
        "inline must not be in a release_bin group. Source the credential from an env file "
        "instead (the kuma-push pattern):\n  " + "\n  ".join(offenders)
    )


def test_the_registry_yields_names():
    """A guard whose name list came back empty would pass for every input."""
    assert len(secret_names()) > 20


def test_discovery_finds_the_converted_group():
    """The half that was missing, and the reason this guard shipped scanning nothing.

    `test_the_registry_yields_names` proves the NAME list is non-empty; nothing proved the
    SOURCE list was. From 2026-08-29 until this test existed, `release_bin_sources()` returned
    `[]` — `release_bin_templates` is a folded Jinja string naming a group, and the old resolver
    kept only dict entries — so the guard passed for every input, including one that renders a
    credential. A guard is only ever observed passing, so the empty-scan case has to be an
    assertion rather than something a reader would have to notice.

    Named rather than counted: asserting a count locks the test to how many scripts happen to be
    converted, and it would be "fixed" by lowering the number.
    """
    found = {Path(src).name for _, src in release_bin_sources()}
    assert "longhorn-backup-health.sh.j2" in found, (
        "release_bin discovery resolved no source for the k3s-backup-health group. The guard "
        f"below cannot see anything it should refuse. Discovered: {sorted(found)}"
    )


def test_every_discovered_source_exists():
    """A src that resolves to no file would be scanned as absent and silently pass."""
    missing = [
        str(src) for _, src in release_bin_sources() if not (REPO / src).is_file()
    ]
    assert not missing, f"release_bin names sources that do not exist: {missing}"


# ── red proofs ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        'TOKEN="{{ secret_rotation_push_token }}"',
        "curl -fsS https://kuma/api/push/{{ arr_autoblock_push_token }}",
        "# rotates authelia_secret on a schedule",
    ],
)
def test_script_rendering_a_secret_is_flagged(body):
    names = [
        "secret_rotation_push_token",
        "arr_autoblock_push_token",
        "authelia_secret",
    ]
    assert scan_for_secrets(body, names)


@pytest.mark.parametrize(
    "body",
    [
        # The pattern these groups are FOR: the token lives in a file the script sources.
        '. /etc/rancher/k3s/kuma-push.env\ncurl -fsS "$KUMA_PUSH_URL"',
        "kubectl -n longhorn-system get volumes -o json",
        "",
        # A longer identifier that merely contains a secret name must not match.
        "AUTHELIA_SECRET_ROTATION_NOTES=1",
    ],
)
def test_script_without_a_secret_is_clean(body):
    names = [
        "secret_rotation_push_token",
        "arr_autoblock_push_token",
        "AUTHELIA_SECRET",
    ]
    assert not scan_for_secrets(body, names)
