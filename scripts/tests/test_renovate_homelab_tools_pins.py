#!/usr/bin/env python3
"""Guard the Renovate manager for `homelab-tools`' pins (target slice H3, PR H3.11).

`hosts/packages/homelab-tools/package.yml` pins sops and chezmoi by version and by the sha256 of
the file each URL serves, once per architecture. The manager must read a version and its digest
in one match, as the nfpm manager does: one that captured only the version would bump it beside
the old digest, and the builder would then refuse the download (reason `tools`).

Run: uv run pytest scripts/tests/test_renovate_homelab_tools_pins.py
"""

import re

from lib import yaml_fast

from _renovate import (
    _CONFIG_MANAGERS,
    _REPO,
    _file_pattern_to_regex,
    _to_python_regex,
)

SPEC = "hosts/packages/homelab-tools/package.yml"
DEPS = {
    "sops": "getsops/sops",
    "chezmoi": "twpayne/chezmoi",
    "nfpm": "goreleaser/nfpm",
    "node-exporter": "prometheus/node_exporter",
    "uv": "astral-sh/uv",
}


def tools_pins(managers: list[dict], text: str) -> list[tuple[str, str, str]]:
    """Each (depName, currentValue, currentDigest) a manager reading SPEC finds in `text`."""
    pins = []
    for mgr in managers:
        if not any(
            _file_pattern_to_regex(fp).search(SPEC) for fp in mgr["managerFilePatterns"]
        ):
            continue
        for ms in mgr["matchStrings"]:
            for m in re.finditer(_to_python_regex(ms), text):
                groups = m.groupdict()
                pins.append(
                    (
                        groups.get("depName") or mgr.get("depNameTemplate") or "",
                        groups.get("currentValue") or "",
                        groups.get("currentDigest") or "",
                    )
                )
    return sorted(pins)


def _spec_pins() -> list[tuple[str, str, str]]:
    raw = yaml_fast.safe_load((_REPO / SPEC).read_text())["tools"]
    return sorted(
        (DEPS[tool], pin["version"], pin["sha256"])
        for tool, arches in raw.items()
        for pin in arches.values()
    )


def test_the_manager_reads_every_version_and_digest_pair_of_the_spec() -> None:
    found = tools_pins(_CONFIG_MANAGERS, (_REPO / SPEC).read_text())
    assert found == _spec_pins()
    # Non-vacuity: five tools, two architectures.
    assert len(found) == 10


def test_a_missing_or_version_only_manager_is_flagged() -> None:
    """The reject halves: no manager at all, and one whose span drops the digest."""
    text = (_REPO / SPEC).read_text()
    others = [
        m
        for m in _CONFIG_MANAGERS
        if not any(
            _file_pattern_to_regex(fp).search(SPEC) for fp in m["managerFilePatterns"]
        )
    ]
    assert tools_pins(others, text) == []
    [manager] = [
        m
        for m in _CONFIG_MANAGERS
        if any(
            _file_pattern_to_regex(fp).search(SPEC) for fp in m["managerFilePatterns"]
        )
    ]
    version_only = {
        **manager,
        "matchStrings": [
            ms.split("[ \\t]*\\n[ \\t]+sha256")[0] for ms in manager["matchStrings"]
        ],
    }
    assert {digest for _, _, digest in tools_pins([version_only], text)} == {""}


def test_an_annotation_not_followed_by_its_version_and_digest_is_not_read() -> None:
    """A block whose digest precedes its version would bump the version beside a stale digest."""
    swapped = (
        "tools:\n  sops:\n    amd64:\n"
        "      # renovate: datasource=github-release-attachments depName=getsops/sops\n"
        f"      sha256: {'a' * 64}\n      version: v1.0.0\n"
    )
    assert tools_pins(_CONFIG_MANAGERS, swapped) == []
