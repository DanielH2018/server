#!/usr/bin/env python3
"""Guard the kind pins in CI's `sops_manifest_type_proof` job (issue #5173).

The job pins the kind binary by version and sha256, and the kindest/node image by tag and
digest. Before #5173 no manager read any of the four, so they aged with no signal. Each manager
must read its version and its digest in one match: one that captured only the version would
bump it beside the old digest, and the job's `sha256sum -c` or image pull would fail.

Run: uv run pytest scripts/tests/test_renovate_kind_pins.py
"""

import copy
import re

from lib import yaml_fast

from _renovate import (
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _file_pattern_to_regex,
    _resolve_setting,
    _to_python_regex,
)

CI_WORKFLOW = ".github/workflows/ci.yml"
KIND = "kubernetes-sigs/kind"
NODE = "kindest/node"


def kind_pins(
    managers: list[dict], text: str
) -> list[tuple[str, str | None, str | None, str | None]]:
    """Each (depName, datasource, currentValue, currentDigest) a kind manager reads from ci.yml."""
    pins = []
    for mgr in managers:
        if mgr.get("depNameTemplate") not in (KIND, NODE) or not any(
            _file_pattern_to_regex(fp).search(CI_WORKFLOW)
            for fp in mgr["managerFilePatterns"]
        ):
            continue
        for ms in mgr["matchStrings"]:
            for m in re.finditer(_to_python_regex(ms), text):
                groups = m.groupdict()
                pins.append(
                    (
                        mgr["depNameTemplate"],
                        mgr.get("datasourceTemplate"),
                        groups.get("currentValue"),
                        groups.get("currentDigest"),
                    )
                )
    return sorted(pins)


def _proof_env() -> dict[str, str]:
    steps = yaml_fast.safe_load((_REPO / CI_WORKFLOW).read_text())["jobs"][
        "sops_manifest_type_proof"
    ]["steps"]
    env: dict[str, str] = {}
    for step in steps:
        env.update(step.get("env") or {})
    return env


def test_the_kind_managers_read_each_version_and_digest_in_one_match() -> None:
    env = _proof_env()
    tag, digest = env["KIND_NODE_IMAGE"].removeprefix(f"{NODE}:").split("@")
    assert kind_pins(_CONFIG_MANAGERS, (_REPO / CI_WORKFLOW).read_text()) == [
        (NODE, "docker", tag, digest),
        (KIND, "github-release-attachments", env["KIND_VERSION"], env["KIND_SHA256"]),
    ]


def test_a_missing_or_version_only_kind_manager_is_flagged() -> None:
    """The reject halves: no manager at all, and one whose span drops the digest."""
    text = (_REPO / CI_WORKFLOW).read_text()
    others = [
        m for m in _CONFIG_MANAGERS if m.get("depNameTemplate") not in (KIND, NODE)
    ]
    assert kind_pins(others, text) == []
    kind = next(m for m in _CONFIG_MANAGERS if m.get("depNameTemplate") == KIND)
    version_only = {
        **kind,
        "matchStrings": [
            ms.split("[ \\t]*\\n[ \\t]+KIND_SHA256")[0] for ms in kind["matchStrings"]
        ],
    }
    [(_, _, _, digest)] = kind_pins([version_only], text)
    assert digest is None


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
