#!/usr/bin/env python3
"""Guard the cosign release pin in the promote, prune and flux-deletion-drill workflows.

Each workflow pins cosign in env as `COSIGN_VERSION` and `COSIGN_SHA256`, and its jobs fetch
that release through a cache keyed on the pin (#7781). A custom manager reads each pin from the
`# renovate:` comment on the line above it.

The cosign version decides the signature format Flux's source-controller verifies, so a bump
must not automerge (#4767). These tests hold three things. The manager finds every pin, version
and digest in one match, and every pin equals promote.yml's. Each pin resolves to the one
manual cosign group. And the rule stays off any other `sigstore/` dep.

Run: uv run pytest scripts/tests/test_renovate_cosign_release.py
"""

import re

import pytest

from _renovate import (
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _file_pattern_to_regex,
    _resolve_setting,
    _to_python_regex,
)

WORKFLOW = ".github/workflows/promote.yml"
# Named rather than derived: a workflow that lost its pin fails here instead of shrinking the
# census and passing. Each value counts the jobs whose fetch reads the file's one pin.
COSIGN_PINS = {
    WORKFLOW: 2,
    ".github/workflows/prune-artifacts.yml": 1,
    ".github/workflows/flux-deletion-drill.yml": 1,
}
FETCH = "sigstore/cosign/releases/download/$COSIGN_VERSION/"
ANNOTATION = "# renovate: datasource=github-release-attachments depName=sigstore/cosign"

UPDATE_TYPES = ("major", "minor", "patch", "digest", "pinDigest")


def _manager() -> dict:
    (manager,) = [
        m
        for m in _CONFIG_MANAGERS
        if any("depName=sigstore/cosign" in ms for ms in m.get("matchStrings", []))
    ]
    return manager


def _matches(text: str) -> list[re.Match]:
    return [
        m
        for ms in _manager()["matchStrings"]
        for m in re.finditer(_to_python_regex(ms), text)
    ]


def _pin(workflow: str) -> tuple[str, str]:
    (match,) = _matches((_REPO / workflow).read_text(encoding="utf-8"))
    return match["currentValue"], match["currentDigest"]


@pytest.mark.parametrize("workflow", sorted(COSIGN_PINS))
def test_the_manager_reads_every_cosign_pin(workflow: str) -> None:
    patterns = _manager().get("managerFilePatterns", [])
    assert any(_file_pattern_to_regex(fp).search(workflow) for fp in patterns), patterns
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert text.count(FETCH) == COSIGN_PINS[workflow]
    assert _pin(workflow) == _pin(WORKFLOW)


@pytest.mark.parametrize("workflow", sorted(COSIGN_PINS))
def test_a_cosign_pin_without_its_annotation_is_flagged(workflow: str) -> None:
    text = (_REPO / workflow).read_text(encoding="utf-8")
    stripped = re.sub(rf"\n[ \t]*{re.escape(ANNOTATION)}", "", text)
    assert _matches(text)
    assert _matches(stripped) == []


def test_a_drifted_cosign_pin_is_flagged() -> None:
    text = (_REPO / WORKFLOW).read_text(encoding="utf-8")
    version, _ = _pin(WORKFLOW)
    drifted = text.replace(f"COSIGN_VERSION: {version}", "COSIGN_VERSION: v0.0.1")
    (match,) = _matches(drifted)
    assert (match["currentValue"], match["currentDigest"]) != _pin(WORKFLOW)


@pytest.mark.parametrize("workflow", sorted(COSIGN_PINS))
@pytest.mark.parametrize("update_type", UPDATE_TYPES)
def test_a_cosign_bump_is_manual_and_never_digest_pinned(
    workflow: str, update_type: str
) -> None:
    manager = _manager()

    def resolve(field: str):
        return _resolve_setting(
            field,
            manager["depNameTemplate"],
            workflow,
            update_type,
            manager["datasourceTemplate"],
            manager="custom.regex",
        )

    assert resolve("automerge") is False
    assert resolve("pinDigests") is False
    assert (resolve("groupName") or "").startswith("cosign (manual")


def test_the_cosign_rule_reaches_only_the_cosign_pin() -> None:
    """Only the exact package name keeps the rule off another `sigstore/` dep.

    A `/sigstore\\/cosign/` regex in `matchPackageNames` would reach
    `sigstore/cosign-installer` and fails here. The walk is the rule alone, with the pin's own
    manager and datasource, so neither can be what keeps the other dep out.
    """
    (rule,) = [
        r
        for r in _PACKAGE_RULES
        if str(r.get("groupName", "")).startswith("cosign (manual")
    ]
    manager = _manager()

    def walk(dep_name: str):
        return _resolve_setting(
            "automerge",
            dep_name,
            WORKFLOW,
            "patch",
            manager["datasourceTemplate"],
            rules=[rule],
            manager="custom.regex",
        )

    assert walk(manager["depNameTemplate"]) is False
    assert walk("sigstore/cosign-installer") is None
