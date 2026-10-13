#!/usr/bin/env python3
"""Guard the cosign binary pin in the promote and prune workflows (#4767).

Each `sigstore/cosign-installer` step pins the binary through its `cosign-release:` input.
Renovate's built-in github-actions manager reads that input itself: its known-actions table maps
the installer to `sigstore/cosign` on github-releases, so the pin is tracked with no custom
manager. A local `renovate --platform=local --dry-run=lookup` (44.155.1) extracted all three
steps, and the Dependency Dashboard lists them.

What the built-in manager does not settle is how a bump lands. The top-level `pinDigests: true`
reached the dep and asked to pin the release's commit into an input that holds only a tag, so
`Pin dependency sigstore/cosign` errored on every run. And the cosign version decides the
signature format Flux's source-controller verifies, so a bump must not automerge. One
packageRule settles both, and these tests hold it to that through the same rule walk the other
Renovate guards use.

Run: uv run pytest scripts/tests/test_renovate_cosign_release.py
"""

import re

import pytest
from lib import yaml_fast

from _renovate import (
    _MANAGERS,
    _REPO,
    _file_pattern_to_regex,
    _resolve_setting,
    _to_python_regex,
)

# Named rather than derived: a workflow that lost its installer step fails here instead of
# shrinking the census to nothing and passing. The count is installer steps per file.
COSIGN_WORKFLOWS = {
    ".github/workflows/promote.yml": 2,
    ".github/workflows/prune-artifacts.yml": 1,
}

# The dep the built-in manager extracts from `cosign-release:`, as the lookup reported it.
DEP = {
    "dep_name": "sigstore/cosign",
    "datasource": "github-releases",
    "dep_type": "uses-with",
    "manager": "github-actions",
}

UPDATE_TYPES = ("major", "minor", "patch", "pinDigest")


def _cosign_releases(rel_path: str) -> list[str | None]:
    wf = yaml_fast.safe_load((_REPO / rel_path).read_text(encoding="utf-8"))
    return [
        (step.get("with") or {}).get("cosign-release")
        for job in wf["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("sigstore/cosign-installer@")
    ]


@pytest.mark.parametrize("rel_path", sorted(COSIGN_WORKFLOWS))
def test_every_installer_step_pins_a_cosign_release(rel_path: str) -> None:
    releases = _cosign_releases(rel_path)
    assert len(releases) == COSIGN_WORKFLOWS[rel_path], releases
    assert all(re.fullmatch(r"v\d+\.\d+\.\d+", r or "") for r in releases), releases


@pytest.mark.parametrize("rel_path", sorted(COSIGN_WORKFLOWS))
@pytest.mark.parametrize("update_type", UPDATE_TYPES)
def test_a_cosign_bump_is_manual_and_never_digest_pinned(
    rel_path: str, update_type: str
) -> None:
    def resolve(field: str):
        return _resolve_setting(
            field,
            DEP["dep_name"],
            rel_path,
            update_type,
            DEP["datasource"],
            dep_type=DEP["dep_type"],
            manager=DEP["manager"],
        )

    assert resolve("automerge") is False
    assert resolve("pinDigests") is False
    assert (resolve("groupName") or "").startswith("cosign (manual")


def test_the_installer_action_keeps_its_digest_pin() -> None:
    """The rule's exact package name must not reach the action's own `uses:` pin."""
    for rel_path in COSIGN_WORKFLOWS:
        assert (
            _resolve_setting(
                "pinDigests",
                "sigstore/cosign-installer",
                rel_path,
                "pinDigest",
                "github-tags",
                dep_type="action",
                manager="github-actions",
            )
            is not False
        )


def test_no_custom_manager_also_extracts_the_cosign_release() -> None:
    """A second extraction of the same span opens a duplicate PR outside the manual rule."""
    for rel_path in COSIGN_WORKFLOWS:
        text = (_REPO / rel_path).read_text(encoding="utf-8")
        for manager in _MANAGERS:
            if not any(
                _file_pattern_to_regex(fp).search(rel_path)
                for fp in manager.get("managerFilePatterns", [])
            ):
                continue
            for ms in manager.get("matchStrings", []):
                for m in re.finditer(_to_python_regex(ms), text):
                    assert "cosign-release" not in m.group(0), (
                        f"{manager.get('description', '')[:60]!r} matches {m.group(0)!r}"
                    )
