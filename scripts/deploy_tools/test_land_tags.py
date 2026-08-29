#!/usr/bin/env python3
"""Tests for land_tags.py -- deriving deploy tags from a merged PR's own file list.

The failure this guards is a SILENTLY NARROWED tag list. `gh pr view --json files`
paginates at 100, so a large PR returns a truncated list that looks complete, and the
deploy then covers a subset of what merged while every check reads green. That is the
"a derivation can narrow the list it replaces" class, and it is invisible from the
passing side -- so every rule here has a reject half.

Run: uv run pytest scripts/deploy_tools/test_land_tags.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import land_tags  # noqa: E402 — needs the path insert above


def test_small_pr_scopes_to_its_services():
    files = [
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "ansible/roles/k8s/radarr/tasks/main.yml",
        "docs/index.md",
    ]
    tags, source = land_tags.derive(files, changed_files=3)
    assert tags == ["radarr", "sonarr"]
    assert source == "pr"


def test_truncated_file_list_is_flagged_and_falls_back():
    """100 returned against 137 changed: the list is truncated. The tags returned
    alongside `fallback` are empty on purpose -- a partial list is worse than none,
    because it looks like an answer."""
    files = [f"ansible/roles/k8s/sonarr/f{i}.yaml" for i in range(100)]
    tags, source = land_tags.derive(files, changed_files=137)
    assert source == "fallback"
    assert tags == []


def test_docs_only_pr_scopes_to_nothing_without_falling_back():
    """Zero tags is a real answer when the count agrees -- it means nothing to deploy,
    not that the derivation failed."""
    tags, source = land_tags.derive(["docs/index.md"], changed_files=1)
    assert tags == []
    assert source == "pr"


def test_a_docker_role_derives_its_tag():
    tags, source = land_tags.derive(
        ["ansible/roles/containers/dozzle/templates/docker-compose.yml.j2"],
        changed_files=1,
    )
    assert tags == ["dozzle"]
    assert source == "pr"


def test_the_shared_common_role_is_not_a_tag():
    """`common` is the shared Docker deploy path, not a service. Deploying `--tags common`
    matches nothing and Ansible exits 0, so a green run would prove nothing happened."""
    tags, _ = land_tags.derive(
        ["ansible/roles/containers/common/tasks/main.yml"], changed_files=1
    )
    assert tags == []


def test_an_archived_role_is_not_a_tag():
    tags, _ = land_tags.derive(
        ["ansible/roles/containers/archive/kopia/tasks/main.yml"], changed_files=1
    )
    assert tags == []


def test_tag_for_rejects_a_path_outside_the_role_trees():
    assert land_tags.tag_for("ansible/inventory/host_vars/daniel-box.yml") is None


def test_missing_changed_files_count_falls_back_rather_than_guessing():
    """`gh` omitting changedFiles must not be read as agreement. land_tags is handed -1
    in that case, which can never equal a real length."""
    tags, source = land_tags.derive(["docs/index.md"], changed_files=-1)
    assert source == "fallback"
    assert tags == []
