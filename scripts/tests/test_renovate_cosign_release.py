#!/usr/bin/env python3
"""Guard the cosign release pin in the promote, prune and flux-deletion-drill workflows.

Each workflow pins cosign in env as `COSIGN_VERSION` and `COSIGN_SHA256`, and its jobs fetch
that release through a cache keyed on the pin. A custom manager reads each pin from the
`# renovate:` comment on the line above it; Renovate's extract, held against
`scripts/validate/renovate_expected_deps.tsv` in CI, proves it reads version and digest in one
match.

The cosign version decides the signature format Flux's source-controller verifies, so a bump
must not automerge. These tests hold three things. Every pin is in the table with its digest
and equals promote.yml's. Each pin resolves to the one manual cosign group. And the rule stays
off any other `sigstore/` dep.

Run: uv run pytest scripts/tests/test_renovate_cosign_release.py
"""

import pytest

from _renovate import (
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _resolve_setting,
    expected_deps,
    workflow_env_pins,
)

WORKFLOW = ".github/workflows/promote.yml"
# Named rather than derived: a workflow that lost its pin fails here instead of shrinking the
# census and passing. Each value counts the jobs whose fetch reads the file's one pin, inline
# or through .github/actions/pinned-tool.
COSIGN_PINS = {
    WORKFLOW: 2,
    ".github/workflows/prune-artifacts.yml": 1,
    ".github/workflows/flux-deletion-drill.yml": 1,
}
FETCH = "sigstore/cosign/releases/download/$COSIGN_VERSION/"
ACTION_FETCH = "version: ${{ env.COSIGN_VERSION }}"

UPDATE_TYPES = ("major", "minor", "patch", "digest", "pinDigest")


def _manager() -> dict:
    (manager,) = [
        m
        for m in _CONFIG_MANAGERS
        if any("depName=sigstore/cosign" in ms for ms in m.get("matchStrings", []))
    ]
    return manager


@pytest.mark.parametrize("workflow", sorted(COSIGN_PINS))
def test_every_cosign_pin_is_extracted_with_its_digest_and_equals_promotes(
    workflow: str,
) -> None:
    assert [d.digest for d in expected_deps(workflow, "sigstore/cosign")] == ["digest"]
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert text.count(FETCH) + text.count(ACTION_FETCH) == COSIGN_PINS[workflow]
    pins = workflow_env_pins(workflow, "COSIGN_VERSION", "COSIGN_SHA256")
    assert pins == workflow_env_pins(WORKFLOW, "COSIGN_VERSION", "COSIGN_SHA256")
    assert len(pins) == 1


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
