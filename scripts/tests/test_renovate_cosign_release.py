#!/usr/bin/env python3
"""Guard the cosign release pin in the promote, prune and flux-deletion-drill workflows.

Each workflow pins cosign in env as `COSIGN_VERSION` and `COSIGN_SHA256`, and its jobs fetch
that release through a cache keyed on the pin. A custom manager reads each pin from the
`# renovate:` comment on the line above it; Renovate's extract, held against
`scripts/validate/renovate_expected_deps.tsv` in CI, proves it reads version and digest in one
match.

The cosign version decides the signature format Flux's source-controller verifies, so a bump
must not automerge. This module holds every pin in the table with its digest and equal to
promote.yml's. The `cosign-` rows of _renovate_rule_rows.py hold each pin in the one
manual cosign group, and the rule off any other `sigstore/` dep.

Run: uv run pytest scripts/tests/test_renovate_cosign_release.py
"""

import pytest

from _renovate import _REPO, expected_deps, workflow_env_pins

WORKFLOW = ".github/workflows/promote.yml"
# Named rather than derived: a workflow that lost its pin fails here instead of shrinking the
# census.
COSIGN_PINS = {
    WORKFLOW: 2,
    ".github/workflows/prune-artifacts.yml": 1,
    ".github/workflows/flux-deletion-drill.yml": 1,
}
FETCH = "sigstore/cosign/releases/download/$COSIGN_VERSION/"
ACTION_FETCH = "version: ${{ env.COSIGN_VERSION }}"


@pytest.mark.parametrize("workflow", sorted(COSIGN_PINS))
def test_every_cosign_pin_is_extracted_with_its_digest_and_equals_promotes(
    workflow: str,
) -> None:
    # The datasource the `cosign-` rows of _renovate_rule_rows.py resolve against.
    assert [
        (d.datasource, d.digest) for d in expected_deps(workflow, "sigstore/cosign")
    ] == [("github-release-attachments", "digest")]
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert text.count(FETCH) + text.count(ACTION_FETCH) == COSIGN_PINS[workflow]
    pins = workflow_env_pins(workflow, "COSIGN_VERSION", "COSIGN_SHA256")
    assert pins == workflow_env_pins(WORKFLOW, "COSIGN_VERSION", "COSIGN_SHA256")
    assert len(pins) == 1
