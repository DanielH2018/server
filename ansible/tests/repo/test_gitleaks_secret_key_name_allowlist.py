"""The `.gitleaks.toml` allowlist for a rendered Secret key name stays as narrow as its reason.

promote's render job scans the publish bundle with gitleaks, and `generic-api-key` reads a
workload's `key: <secret key name>` line as a credential (#5623's monitor-bridge flip tripped
it on the B2 probe's Secret item name). The allowlist admits that line shape and nothing
wider: one rule, the bundle's manifests only, and a snake_case name with at least one
underscore. A credential on the same line has no underscore, so it still trips.

The sample lines below trip the pre-commit scan themselves, so each one that does carries a
`gitleaks:allow` marker.

Run: uv run pytest ansible/tests/repo/test_gitleaks_secret_key_name_allowlist.py
"""

import re
import tomllib

import pytest
from _helpers import REPO

CONFIG = tomllib.loads((REPO / ".gitleaks.toml").read_text())


def _entry() -> dict:
    [entry] = [a for a in CONFIG["allowlists"] if a.get("regexTarget") == "line"]
    return entry


def admits(line: str) -> bool:
    """Whether the allowlist's line regex exempts `line`."""
    return any(re.search(r, line) for r in _entry()["regexes"])


def test_the_entry_needs_both_its_path_and_its_line_for_one_rule():
    entry = _entry()
    assert entry["condition"] == "AND"
    assert entry["targetRules"] == ["generic-api-key"]
    assert entry["paths"] == [r"^out/cluster/[^/]+/[^/]+\.yaml$"]


@pytest.mark.parametrize(
    "line",
    [
        "          - key: b2_probe_application_key",  # gitleaks:allow
        "          - key: ha_token",
        "              key: monitor_bridge_discord_push_token",
    ],
)
def test_a_secret_key_name_is_admitted(line):
    assert admits(line)


@pytest.mark.parametrize(
    "line",
    [
        "          - key: 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",  # gitleaks:allow
        "          - key: K005aBcDeFgHiJkLmNoPqRsTuVwXyZ01",
        "          - key: abcdefghijklmnopqrstuvwxyz012345",
        "          - key: b2_probe_application_key K005aBcDeFgHiJkLmNoPqRs",  # gitleaks:allow
        "          value: b2_probe_application_key",
    ],
)
def test_a_credential_on_a_key_line_is_not_admitted(line):
    assert not admits(line)
