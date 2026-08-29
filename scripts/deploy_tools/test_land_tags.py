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


# Comment lines are stripped: land.sh's own comment explains why `deploy.sh --changed` is
# NOT used, and matching that sentence would fail the very rule it documents.
_LAND_SH = "\n".join(
    line
    for line in (Path(__file__).resolve().parent / "land.sh").read_text().splitlines()
    if not line.lstrip().startswith("#")
)


def test_land_health_checks_the_tags_it_deployed():
    """land.sh must resolve the fallback path to a tag list itself, not hand deploy.sh
    `--changed`. deploy.sh resolves --changed internally, so the verdict call downstream
    would receive an empty --tags -- and gate() with no tags reports settled having health
    checked nothing, on exactly the large-PR path where verification matters most."""
    assert "deploy.sh --changed" not in _LAND_SH


def test_land_still_invokes_the_deployer():
    """The reject half. A test for an absent string passes identically against an empty
    file or a renamed script, so pin what must be present too."""
    assert "./scripts/deploy.sh --tags" in _LAND_SH


def test_a_build_role_pulls_in_the_workload_that_runs_its_image():
    """PR #570's real shape: Renovate bumped only the two n8n Dockerfiles. Deriving
    `n8n-images` alone builds the images and rolls nothing, because k8s_rebuilt_images is
    play-scoped -- the 2026-08-08 `@n8n/di` failure. Caught landing #570 on 2026-08-29."""
    files = [
        "ansible/roles/k8s/n8n-images/templates/Dockerfile.j2",
        "ansible/roles/k8s/n8n-images/templates/Dockerfile-runners.j2",
    ]
    tags, source = land_tags.derive(files, changed_files=2)
    assert source == "pr"
    assert tags == ["n8n", "n8n-images"]


def test_an_ordinary_role_is_not_widened():
    """The reject half. A deriver that widened everything would pass the test above."""
    tags, source = land_tags.derive(
        ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"], changed_files=1
    )
    assert source == "pr"
    assert tags == ["sonarr"]


def test_land_preflights_before_waiting_on_ci():
    """Order is the entire value. Checking blockers AFTER the CI wait would still catch the
    condition but keep the ~6 wasted minutes that motivated it (PR #570, 2026-08-29)."""
    preflight = _LAND_SH.index("deploy_tags.py blockers")
    ci_wait = _LAND_SH.index("await_ci.py")
    assert preflight < ci_wait, "land.sh waits on CI before checking for blockers"


def test_land_treats_a_stale_tree_as_a_resume_point():
    """deploy.sh exit 4 means nothing was deployed and the tree is behind — CLAUDE.md calls
    that a resume point. Reporting it as deploy-failed sends the operator after a fault that
    is not there."""
    assert 'deploy_rc" -eq 4' in _LAND_SH


def test_land_never_bypasses_the_staleness_guard():
    """The reject half of the retry: the tempting fix for exit 4 is the flag that disables
    the check, which deploys stale templates over live config."""
    assert "--skip-staleness-check" not in _LAND_SH


def test_a_setup_plane_pr_is_not_nothing_to_deploy():
    """PR #587's real shape: no k8s or containers role, so zero deploy tags — but it changed
    the deployer and needed `initial_setup.yml --tags gitops_deploy` by hand. land.sh reported
    `nothing-to-deploy` and exited 0 (2026-08-29)."""
    files = [
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
        "scripts/deploy_tools/land.sh",
        "mkdocs.yml",
    ]
    tags, _ = land_tags.derive(files, changed_files=3)
    assert tags == [], "no service tag should be derived from these paths"
    assert "initial_setup.yml" in land_tags.plane_note(files)


def test_an_ordinary_service_pr_needs_no_manual_apply():
    """The reject half. A classifier that always returned a remediation would make every
    landing report needs-manual-apply and train the operator to ignore it."""
    assert (
        land_tags.plane_note(["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"])
        == ""
    )


def test_a_mixed_pr_reports_both_a_tag_and_a_manual_apply():
    """The harder silence: the deploy genuinely succeeds and half the change is unapplied, so
    a tag-carrying PR must still surface the setup-plane half."""
    files = [
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
    ]
    tags, _ = land_tags.derive(files, changed_files=2)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) != ""


def test_land_reports_a_setup_plane_pr_as_unfinished():
    assert "needs-manual-apply" in _LAND_SH


def test_land_still_has_a_nothing_to_deploy_path():
    """The reject half of the verdict change: a docs-only PR really is finished, and must not
    be reported as needing a human."""
    assert "nothing-to-deploy" in _LAND_SH
