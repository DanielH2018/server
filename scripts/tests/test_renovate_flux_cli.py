#!/usr/bin/env python3
"""Guard the flux CLI pin in the CI workflows (#4822, #7778).

Each workflow that runs the flux CLI pins it in env as `FLUX_VERSION` and `FLUX_SHA256`, and
its jobs fetch that release through a cache keyed on the pin.
tests/repo/test_promote_workflow.py holds the promote workflow's pin equal to
`flux_version` in k8s/roles/flux, and the census below holds every workflow's pin equal to it
and to the amd64 digest in homelab-tools. A custom manager reads each pin from the
`# renovate:` comment on the line above it.

These tests hold two things. The manager finds every pin, version and digest in one match. And
each pin and `flux_version` resolve to the one manual flux group, so a bump opens as one PR.

Run: uv run pytest scripts/tests/test_renovate_flux_cli.py
"""

import re

import pytest
from lib import yaml_fast

from _renovate import (
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _file_pattern_to_regex,
    _resolve_setting,
    _to_python_regex,
)

WORKFLOW = ".github/workflows/promote.yml"
FLUX_DEFAULTS = "k8s/roles/flux/defaults/main.yml"
DEP_NAME = "fluxcd/flux2"
HOMELAB_TOOLS = "hosts/packages/homelab-tools/package.yml"

# Named rather than derived: a workflow that lost its pin fails here instead of shrinking the
# census and passing. Each value counts the jobs whose fetch reads the file's one pin.
FLUX_PINS = {
    WORKFLOW: 5,
    ".github/workflows/ci.yml": 2,
    ".github/workflows/bootstrap-drill.yml": 1,
    ".github/workflows/flux-deletion-drill.yml": 1,
}
FETCH = "fluxcd/flux2/releases/download/$FLUX_VERSION/"

# The deps named `fluxcd/flux2` other than the workflow pin, as the extract reports each:
# (file, datasource, manager, depType). `_dep` derives the workflow pin from its manager.
DEPS = {
    "flux_version": (FLUX_DEFAULTS, "github-releases", "custom.regex", "annotated-pin"),
    "the homelab-tools pin": (
        HOMELAB_TOOLS,
        "github-release-attachments",
        "custom.regex",
        None,
    ),
}

UPDATE_TYPES = ("major", "minor", "patch", "digest", "pinDigest")


def _manager() -> dict:
    (manager,) = [
        m
        for m in _CONFIG_MANAGERS
        if any(
            _file_pattern_to_regex(fp).search(WORKFLOW)
            for fp in m.get("managerFilePatterns", [])
        )
        and any("depName=fluxcd/flux2" in ms for ms in m.get("matchStrings", []))
    ]
    return manager


def _dep(dep: str) -> tuple[str, str, str, str, str | None]:
    """The dep as (depName, file, datasource, manager, depType).

    A workflow pin's name and datasource come from the manager's own templates, so this walk
    tests what the manager emits rather than a constant that matched the flux rule before the
    manager existed.
    """
    if dep == "the workflow pin":
        manager = _manager()
        return (
            manager.get("depNameTemplate", ""),
            WORKFLOW,
            manager.get("datasourceTemplate", ""),
            "custom.regex",
            None,
        )
    return (DEP_NAME, *DEPS[dep])


def _matches(text: str) -> list[re.Match]:
    return [
        m
        for ms in _manager()["matchStrings"]
        for m in re.finditer(_to_python_regex(ms), text)
    ]


def _flux_version() -> str:
    defaults = yaml_fast.safe_load((_REPO / FLUX_DEFAULTS).read_text(encoding="utf-8"))
    return defaults["flux_version"]


def _amd64_digest() -> str:
    tools = yaml_fast.safe_load((_REPO / HOMELAB_TOOLS).read_text(encoding="utf-8"))
    return tools["tools"]["flux"]["amd64"]["sha256"]


@pytest.mark.parametrize("workflow", sorted(FLUX_PINS))
def test_the_manager_reads_every_flux_cli_pin(workflow: str) -> None:
    patterns = _manager().get("managerFilePatterns", [])
    assert any(_file_pattern_to_regex(fp).search(workflow) for fp in patterns), patterns
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert text.count(FETCH) == FLUX_PINS[workflow]
    matches = _matches(text)
    assert [(m["currentValue"], m["currentDigest"]) for m in matches] == [
        (_flux_version(), _amd64_digest())
    ] * len(re.findall(r"^\s+FLUX_VERSION:", text, re.M))
    assert matches, workflow


@pytest.mark.parametrize("workflow", sorted(FLUX_PINS))
def test_a_flux_pin_without_its_annotation_is_flagged(workflow: str) -> None:
    text = (_REPO / workflow).read_text(encoding="utf-8")
    stripped = re.sub(
        r"\n[ \t]*# renovate: datasource=github-release-attachments depName=fluxcd/flux2",
        "",
        text,
    )
    assert _matches(text)
    assert _matches(stripped) == []


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


@pytest.mark.parametrize("dep", sorted([*DEPS, "the workflow pin"]))
@pytest.mark.parametrize("update_type", UPDATE_TYPES)
def test_every_flux_dep_lands_in_the_manual_flux_group(
    dep: str, update_type: str
) -> None:
    dep_name, rel_path, datasource, manager, dep_type = _dep(dep)

    def resolve(field: str):
        return _resolve_setting(
            field,
            dep_name,
            rel_path,
            update_type,
            datasource,
            dep_type=dep_type,
            manager=manager,
        )

    assert (resolve("groupName") or "").startswith("flux (manual")
    assert resolve("automerge") is False
