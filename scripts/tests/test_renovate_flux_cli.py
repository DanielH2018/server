#!/usr/bin/env python3
"""Guard the flux CLI pin in the promote and CI workflows (#4822).

Each `fluxcd/flux2/action` step installs the flux CLI through its `version:` input, and
ansible/tests/repo/test_promote_workflow.py holds the promote workflow's inputs equal to
`flux_version` in roles/k8s/flux. The CI workflow's `flux_roundtrip` job installs the same CLI
for target slice R2's round trip, and the census below holds its input equal too. Renovate's built-in github-actions manager extracts the action as `fluxcd/flux2`
on github-tags for its commit-SHA pin, but its known-actions table has no entry for the action,
so it never reads the input. A custom manager reads it instead, from a `# renovate:` comment on
the line above each input.

These tests hold three things. The manager finds every input, and its matched span stays off
the action line, whose `# vX.Y.Z` comment belongs to the github-actions dep. And the input, the
action pin and `flux_version` resolve to the one manual flux group, so a bump opens as one PR.

Run: uv run pytest scripts/tests/test_renovate_flux_cli.py
"""

import copy
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
FLUX_DEFAULTS = "ansible/roles/k8s/flux/defaults/main.yml"
DEP_NAME = "fluxcd/flux2"
ACTION = "fluxcd/flux2/action@"

# Named rather than derived: a workflow that lost a flux step fails here instead of shrinking
# the census and passing.
FLUX_STEPS = {WORKFLOW: 4, ".github/workflows/ci.yml": 2}

# The deps named `fluxcd/flux2` other than the version input, as the extract reports each:
# (file, datasource, manager, depType). The action dep's shape is the one #4822's lookup
# recorded. `_dep` derives the version input from its manager.
DEPS = {
    "flux_version": (FLUX_DEFAULTS, "github-releases", "custom.regex", "annotated-pin"),
    "the action pin": (WORKFLOW, "github-tags", "github-actions", "action"),
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

    The version input's name and datasource come from the manager's own templates, so this
    walk tests what the manager emits rather than a constant that matched the flux rule before
    the manager existed.
    """
    if dep == "the version input":
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


@pytest.mark.parametrize("workflow", sorted(FLUX_STEPS))
def test_the_manager_reads_every_flux_cli_input(workflow: str) -> None:
    patterns = _manager().get("managerFilePatterns", [])
    assert any(_file_pattern_to_regex(fp).search(workflow) for fp in patterns), patterns
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert text.count(ACTION) == FLUX_STEPS[workflow]
    matches = _matches(text)
    assert [m.group("currentValue") for m in matches] == [_flux_version()] * FLUX_STEPS[
        workflow
    ]
    assert all(ACTION not in m.group(0) for m in matches), [m.group(0) for m in matches]


@pytest.mark.parametrize("workflow", sorted(FLUX_STEPS))
def test_a_flux_step_without_its_annotation_is_flagged(workflow: str) -> None:
    text = (_REPO / workflow).read_text(encoding="utf-8")
    stripped = re.sub(
        r"\n[ \t]*# renovate: datasource=github-releases depName=fluxcd/flux2", "", text
    )
    assert len(_matches(stripped)) < stripped.count(ACTION)


@pytest.mark.parametrize("dep", sorted([*DEPS, "the version input"]))
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


def test_the_action_pin_outside_the_flux_rule_is_flagged() -> None:
    """Without github-tags in the flux rule, the action pin opens as a PR of its own."""
    rules = copy.deepcopy(_PACKAGE_RULES)
    (rule,) = [
        r for r in rules if str(r.get("groupName", "")).startswith("flux (manual")
    ]
    rule["matchDatasources"] = ["github-releases"]
    _, rel_path, datasource, manager, dep_type = _dep("the action pin")
    group = _resolve_setting(
        "groupName",
        DEP_NAME,
        rel_path,
        "digest",
        datasource,
        rules=rules,
        dep_type=dep_type,
        manager=manager,
    )
    assert not (group or "").startswith("flux (manual")
