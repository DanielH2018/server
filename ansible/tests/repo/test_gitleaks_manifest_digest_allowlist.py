"""The `.gitleaks.toml` allowlist for a host package's `secrets_inputs_sha256` stays narrow.

The `host_packages` job scans each named package root with gitleaks, and `generic-api-key`
reads the manifest's `"secrets_inputs_sha256": "<64 hex>"` line as a credential. The allowlist
admits that one line shape in `usr/lib/homelab/manifest.json` for that one rule. Another key
with a 64-hex value, or a longer value on the same key, still trips.

The sample lines below trip a scan themselves, so each one that does carries a
`gitleaks:allow` marker.

Run: uv run pytest ansible/tests/repo/test_gitleaks_manifest_digest_allowlist.py
"""

import re
import tomllib

import pytest
from _helpers import REPO

CONFIG = tomllib.loads((REPO / ".gitleaks.toml").read_text())
DIGEST = "2678c3451a396ec559a9e8463808ec85c1e6778350f4a4515f1fdfa02d3a83b0"


def _entry() -> dict:
    [entry] = [
        a
        for a in CONFIG["allowlists"]
        if a.get("paths") == [r"^usr/lib/homelab/manifest\.json$"]
    ]
    return entry


def admits(line: str) -> bool:
    return any(re.search(r, line) for r in _entry()["regexes"])


def test_the_entry_needs_both_its_path_and_its_line_for_one_rule():
    entry = _entry()
    assert entry["condition"] == "AND"
    assert entry["targetRules"] == ["generic-api-key"]
    assert entry["regexTarget"] == "line"


@pytest.mark.parametrize(
    "line",
    [
        f' "secrets_inputs_sha256": "{DIGEST}",',  # gitleaks:allow
        f' "secrets_inputs_sha256": "{DIGEST}"',  # gitleaks:allow
    ],
)
def test_the_manifest_digest_line_is_admitted(line):
    assert admits(line)


@pytest.mark.parametrize(
    "line",
    [
        f' "api_token_sha256": "{DIGEST}",',  # gitleaks:allow
        f' "secrets_inputs_sha256": "{DIGEST}0",',  # gitleaks:allow
        ' "secrets_inputs_sha256": "K005aBcDeFgHiJkLmNoPqRsTuVwXyZ01",',  # gitleaks:allow
        f' "secrets_inputs_sha256": "{DIGEST}", "token": "x"',  # gitleaks:allow
    ],
)
def test_another_key_or_value_on_the_line_is_not_admitted(line):
    assert not admits(line)
