#!/usr/bin/env python3
"""Guard the grouping of the kind pins in CI's `sops_manifest_type_proof` job (issue #5173).

The job pins the kind binary by version and sha256, and the kindest/node image by tag and
digest. Renovate's extract, held against `scripts/validate/renovate_expected_deps.tsv` in CI,
proves each manager reads its version and its digest in one match. This holds the two in one
PR, and the node image to patch and digest bumps.

Run: uv run pytest scripts/tests/test_renovate_kind_pins.py
"""

import copy

from _renovate import _PACKAGE_RULES, _resolve_setting

CI_WORKFLOW = ".github/workflows/ci.yml"
KIND = "kubernetes-sigs/kind"
NODE = "kindest/node"


def _group(
    dep: str, datasource: str, update_type: str, rules: list[dict] = _PACKAGE_RULES
):
    return _resolve_setting(
        "groupName", dep, CI_WORKFLOW, update_type, datasource, rules
    )


def test_the_binary_and_the_node_image_share_one_group() -> None:
    assert (
        _group(KIND, "github-release-attachments", "minor")
        == _group(NODE, "docker", "patch")
        == _group(NODE, "docker", "digest")
        == "kind (binary + node image)"
    )


def test_a_kind_rule_before_the_workflows_rule_is_flagged() -> None:
    """Moved ahead of the `.github/workflows/**` rule, the group splits into two `{{depName}}` PRs."""
    rules = copy.deepcopy(_PACKAGE_RULES)
    [index] = [
        i
        for i, r in enumerate(rules)
        if r.get("groupName") == "kind (binary + node image)"
    ]
    rules.insert(0, rules.pop(index))
    assert _group(KIND, "github-release-attachments", "minor", rules) == "{{depName}}"


def test_the_node_image_gets_no_minor_or_major_update() -> None:
    def enabled(update_type: str):
        return _resolve_setting("enabled", NODE, CI_WORKFLOW, update_type, "docker")

    assert enabled("minor") is False
    assert enabled("major") is False
    assert enabled("patch") is None
    assert enabled("digest") is None
