#!/usr/bin/env python3
"""Guard the flux CLI pin in the CI workflows.

Each workflow that runs the flux CLI pins it in env as `FLUX_VERSION` and `FLUX_SHA256`, and
its jobs fetch that release through a cache keyed on the pin.
The `promote-flux-cli-matches-the-controllers` row of tests/repo/test_census_rows_workflows.py
holds the promote workflow's pin equal to `flux_version` in k8s/roles/flux, and the census below
holds every workflow's pin equal to it and to the amd64 digest in homelab-tools. A custom
manager reads each pin from the `# renovate:` comment on the line above it; Renovate's extract,
held against `scripts/validate/renovate_expected_deps.tsv` in CI, proves it reads version and
digest in one match.

This module holds every pin in the table with its digest and equal to the role's
`flux_version` and homelab-tools' amd64 digest. The `flux-` rows of
_renovate_rule_rows.py hold each pin and `flux_version` in the one manual flux group,
so a bump opens as one PR.

Run: uv run pytest scripts/tests/test_renovate_flux_cli.py
"""

import re

import pytest
from lib import yaml_fast

from _renovate import (
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    expected_deps,
    workflow_env_pins,
)

WORKFLOW = ".github/workflows/promote.yml"
FLUX_DEFAULTS = "k8s/roles/flux/defaults/main.yml"
DEP_NAME = "fluxcd/flux2"
HOMELAB_TOOLS = "hosts/packages/homelab-tools/package.yml"

# Named rather than derived: a workflow that lost its pin fails here instead of shrinking the
# census.
FLUX_PINS = {
    WORKFLOW: 5,
    ".github/workflows/ci.yml": 2,
    ".github/workflows/bootstrap-drill.yml": 1,
    ".github/workflows/flux-deletion-drill.yml": 1,
}
FETCH = re.compile(
    r"fluxcd/flux2/releases/download/\$FLUX_VERSION/|^\s+tool: flux$", re.M
)


def _manager() -> dict:
    (manager,) = [
        m
        for m in _CONFIG_MANAGERS
        if any(
            "depName=fluxcd/flux2" in ms and "FLUX_VERSION" in ms
            for ms in m.get("matchStrings", [])
        )
    ]
    return manager


def _flux_version() -> str:
    defaults = yaml_fast.safe_load((_REPO / FLUX_DEFAULTS).read_text(encoding="utf-8"))
    return defaults["flux_version"]


def _amd64_digest() -> str:
    tools = yaml_fast.safe_load((_REPO / HOMELAB_TOOLS).read_text(encoding="utf-8"))
    return tools["tools"]["flux"]["amd64"]["sha256"]


@pytest.mark.parametrize("workflow", sorted(FLUX_PINS))
def test_every_flux_cli_pin_is_extracted_with_its_digest_and_equals_the_roles(
    workflow: str,
) -> None:
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert len(FETCH.findall(text)) == FLUX_PINS[workflow]
    pins = workflow_env_pins(workflow, "FLUX_VERSION", "FLUX_SHA256")
    assert pins and pins == [(_flux_version(), _amd64_digest())] * len(pins)
    extracted = [
        d
        for d in expected_deps(workflow, DEP_NAME)
        if d.datasource == _manager()["datasourceTemplate"]
    ]
    assert [d.digest for d in extracted] == ["digest"] * len(pins)


def test_the_workflow_pin_moves_its_digest_in_the_flux_group() -> None:
    """Only the attachments datasource reads a new release's digest for FLUX_SHA256."""
    (rule,) = [
        r
        for r in _PACKAGE_RULES
        if str(r.get("groupName", "")).startswith("flux (manual")
    ]
    datasource = _manager()["datasourceTemplate"]
    assert datasource == "github-release-attachments"
    assert datasource in rule["matchDatasources"]
