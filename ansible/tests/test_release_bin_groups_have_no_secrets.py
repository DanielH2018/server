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

Run: uv run pytest ansible/tests/test_release_bin_groups_have_no_secrets.py
"""

import re
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
REPO = ANSIBLE.parent
REGISTRY = ANSIBLE / "secret_rotation.yml"

# Names that appear in the registry but are too generic to match on: a substring hit would flag
# every script mentioning the word. Empty today; kept as the documented escape hatch so a future
# generic name is handled here rather than by weakening the regex.
GENERIC_NAMES = frozenset()


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


def _walk(node):
    """Yield every dict nested anywhere in a parsed task file."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def release_bin_sources():
    """Every `src` handed to release_bin.yml anywhere in the tree, as (task_file, src)."""
    found = []
    for path in sorted(ANSIBLE.glob("roles/**/tasks/*.yml")):
        if "/archive/" in str(path):
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for node in _walk(doc):
            for key in ("release_bin_templates", "release_bin_files"):
                for entry in node.get(key) or []:
                    if isinstance(entry, dict) and entry.get("src"):
                        found.append((path.relative_to(REPO), entry["src"]))
    return found


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
