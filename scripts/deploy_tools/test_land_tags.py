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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import land_tags  # noqa: E402 — needs the path insert above

# A fixture, not live inventory. These tests pin the DERIVATION, and reading containers_list
# would make them fail whenever a service is retired -- `dozzle` was in this file until it was
# removed from the cluster on 2026-08-29. The live set is checked once, separately, by
# test_the_shared_roles_are_still_undeclared.
_DECLARED = frozenset(
    {
        "artifacts",
        "autofix-bridge",
        "docs",
        "dozzle",
        "headlamp",
        "home-assistant",
        "homepage",
        "jellyfin",
        "karakeep",
        "media-volume",
        "monitor-bridge",
        "n8n",
        "n8n-images",
        "netpol-baseline",
        "peanut",
        "prowlarr",
        "qbittorrent",
        "radarr",
        "registry",
        "scrutiny",
        "sonarr",
        "terraria-stats",
        "traefik",
        "valheim-stats",
        "zigbee2mqtt",
    }
)


@pytest.fixture(autouse=True)
def _pin_declared(monkeypatch):
    """Pin the declared set for every test here, so a service retirement cannot turn a
    derivation test red for a reason that has nothing to do with the derivation."""
    monkeypatch.setattr(land_tags, "declared_tags", lambda: set(_DECLARED))


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
    the deployer and land.sh reported `nothing-to-deploy` and exited 0 (2026-08-29). The
    deployer applies its own role itself since #719, so the note is empty now and the
    landing is instead verified against the deployer's state (`self_applied` below); what
    still needs a hand is a setup role initial_setup.yml does not include."""
    files = [
        "ansible/roles/setup/k3s/defaults/main.yml",
        "scripts/deploy_tools/land.sh",
        "mkdocs.yml",
    ]
    tags, _ = land_tags.derive(files, changed_files=3)
    assert tags == [], "no service tag should be derived from these paths"
    assert "k3s-bringup.yml" in land_tags.plane_note(files)


def test_a_self_applied_setup_role_is_not_owed_to_a_hand():
    """#723 changed gitops_deploy and renovate_notify — both applied by the tick — and land.sh
    exited 1 with `needs-manual-apply` naming the two playbook runs the next tick was about
    to perform itself (2026-09-01)."""
    files = [
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
        "ansible/roles/setup/renovate_notify/files/renovate_notify.py",
    ]
    assert land_tags.plane_note(files) == ""
    assert land_tags.self_applied(files) is True


def test_a_deploy_plane_change_is_the_ticks_to_apply():
    files = ["ansible/templates/traefik.yml.j2"]
    assert land_tags.plane_note(files) == ""
    assert land_tags.self_applied(files) is True


def test_a_bringup_playbook_is_still_owed_to_a_hand():
    """The reject half of `self_applied`: the tick parks on these outright, so the deployer's
    state can never say they were applied."""
    files = ["ansible/bootstrap.yml"]
    assert land_tags.plane_note(files) != ""
    assert land_tags.self_applied(files) is False


def test_docs_and_ordinary_services_are_not_self_applied():
    for files in (
        ["docs/gitops-pipeline.md"],
        ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"],
        ["ansible/roles/setup/k3s/defaults/main.yml"],
    ):
        assert land_tags.self_applied(files) is False, files


def test_an_ordinary_service_pr_needs_no_manual_apply():
    """The reject half. A classifier that always returned a remediation would make every
    landing report needs-manual-apply and train the operator to ignore it."""
    assert (
        land_tags.plane_note(["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"])
        == ""
    )


def test_a_mixed_pr_reports_both_a_tag_and_a_manual_apply():
    """The harder silence: the deploy genuinely succeeds and half the change is unapplied, so
    a tag-carrying PR must still surface the half a hand owes."""
    files = [
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "ansible/roles/setup/k3s/defaults/main.yml",
    ]
    tags, _ = land_tags.derive(files, changed_files=2)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) != ""


def test_land_reports_a_setup_plane_pr_as_unfinished():
    assert "needs-manual-apply" in _LAND_SH


def test_land_reads_the_deployers_state_for_a_self_applied_pr():
    """A self-applied PR has no service tag, so nothing but the deployer's own markers can say
    whether the tick applied it. Both must be read: `behind_since` for "not yet", `hold_sha`
    for "tried and failed"."""
    assert "--self-applied" in _LAND_SH
    assert "behind_since" in _LAND_SH and "hold_sha" in _LAND_SH
    assert "VERDICT: deferred" in _LAND_SH


def test_land_retries_a_tick_skipped_for_lock_contention():
    """gitops_tick.sh exit 3 means the unit's own flock gave up and NOTHING fast-forwarded.
    #723's landing carried on from there and every later reading said "the tick deferred"
    (2026-09-01). The retry loop must wrap the tick, not just the deploy."""
    tick = _LAND_SH.index("gitops_tick.sh")
    loop = _LAND_SH.rfind('while [ "$attempt" -le "$LOCK_RETRIES" ]', 0, tick)
    assert loop != -1, "the first gitops_tick.sh call is not inside a retry loop"
    assert '"$tick_rc" -ne 3' in _LAND_SH


# PR #617's real 32-path file list, read from `gh pr view 617 --json files` on 2026-08-29.
# 22 of its role directories have a containers_list entry; `manifests` and `volume-claim` do
# not, and naming either in --tags makes deploy.sh refuse the whole list.
_PR_617_FILES = [
    "ansible/roles/k8s/artifacts/defaults/main.yml",
    "ansible/roles/k8s/autofix-bridge/defaults/main.yml",
    "ansible/roles/k8s/docs/defaults/main.yml",
    "ansible/roles/k8s/headlamp/defaults/main.yml",
    "ansible/roles/k8s/home-assistant/defaults/main.yml",
    "ansible/roles/k8s/homepage/defaults/main.yml",
    "ansible/roles/k8s/jellyfin/defaults/main.yml",
    "ansible/roles/k8s/karakeep/defaults/main.yml",
    "ansible/roles/k8s/manifests/defaults/main.yml",
    "ansible/roles/k8s/manifests/tasks/main.yml",
    "ansible/roles/k8s/manifests/tasks/release_stamp.yml",
    "ansible/roles/k8s/media-volume/defaults/main.yml",
    "ansible/roles/k8s/monitor-bridge/defaults/main.yml",
    "ansible/roles/k8s/n8n/defaults/main.yml",
    "ansible/roles/k8s/netpol-baseline/defaults/main.yml",
    "ansible/roles/k8s/peanut/defaults/main.yml",
    "ansible/roles/k8s/prowlarr/defaults/main.yml",
    "ansible/roles/k8s/qbittorrent/defaults/main.yml",
    "ansible/roles/k8s/registry/defaults/main.yml",
    "ansible/roles/k8s/scrutiny/defaults/main.yml",
    "ansible/roles/k8s/volume-claim/defaults/main.yml",
    "ansible/roles/k8s/sonarr/defaults/main.yml",
    "ansible/roles/k8s/terraria-stats/defaults/main.yml",
    "ansible/roles/k8s/traefik/defaults/main.yml",
    "ansible/roles/k8s/valheim-stats/defaults/main.yml",
    "ansible/roles/k8s/zigbee2mqtt/defaults/main.yml",
    "ansible/roles/setup/k3s/defaults/main.yml",
    "ansible/tests/test_base_images_digest_pinned.py",
    "docs/claude-tooling.md",
    "scripts/diagnostics/probe.py",
    "scripts/diagnostics/probe_releases.py",
    "scripts/diagnostics/test_probe_releases.py",
]


def test_pr_617_derives_its_services_and_not_the_shared_roles():
    """The measured failure. `manifests` and `volume-claim` have no containers_list entry, so
    including them made deploy.sh exit 2 and refuse the 22 valid services beside them --
    land.sh printed nothing-to-deploy and 22 digest pins sat undeployed (2026-08-29)."""
    tags, source = land_tags.derive(_PR_617_FILES, changed_files=len(_PR_617_FILES))
    assert source == "pr"
    assert "manifests" not in tags
    assert "volume-claim" not in tags
    assert len(tags) == 22
    assert {"sonarr", "jellyfin", "traefik", "n8n"} <= set(tags)


def test_pr_617_reports_the_shared_roles_as_owed_work():
    """Dropping them from the tags is only half the fix. Dropping them from the REPORT too is
    the setup-plane silence again: landed, unapplied, and nothing says so."""
    note = land_tags.plane_note(_PR_617_FILES)
    assert "manifests" in note
    assert "volume-claim" in note
    assert "ansible/deploy.yml" in note, (
        "a full deploy is the only thing that applies them"
    )
    # `k3s-bringup.yml`, NOT `initial_setup.yml`, which this asserted until 2026-09-01. The
    # k3s role appears only in the bring-up playbook, so the old expectation was pinning a
    # command that exits 0 having matched no task — see `_SETUP_ROLES_OUTSIDE_INITIAL_SETUP`.
    assert "ansible/k3s-bringup.yml --tags k3s" in note, (
        "roles/setup/k3s is in this PR too"
    )


def test_a_shared_role_alone_derives_no_tag_and_still_reports():
    files = ["ansible/roles/k8s/manifests/tasks/main.yml"]
    tags, source = land_tags.derive(files, changed_files=1)
    assert (tags, source) == ([], "pr")
    assert land_tags.plane_note(files) != ""


def test_an_ordinary_service_role_is_neither_dropped_nor_reported():
    """The reject half of both rules above. A splitter that called every role shared would
    derive no tags at all and report a full deploy on every landing."""
    files = ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"]
    tags, _ = land_tags.derive(files, changed_files=1)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) == ""


def test_the_shared_roles_are_still_undeclared(monkeypatch):
    """The one test that reads live inventory. The split is only correct while these names
    really have no containers_list entry -- give one an entry and it becomes an ordinary
    deployable role, and this test is where that gets noticed."""
    monkeypatch.undo()
    declared = land_tags.declared_tags()
    assert "manifests" not in declared
    assert "volume-claim" not in declared
    assert "sonarr" in declared, (
        "the reject half: a lookup returning nothing would pass"
    )


def test_land_does_not_call_a_refused_tag_list_nothing_to_deploy():
    """deploy.sh exit 2 means it refused the WHOLE list and deployed nothing, including every
    valid service beside the bad tag. Reporting that as nothing-to-deploy and exiting 0 is
    what hid PR #617. Matched literally: a guard read through a variable stops matching
    silently."""
    assert 'echo "VERDICT: nothing-to-deploy (no service tag matched)"' not in _LAND_SH
    assert "deploy-failed (PR #$PR — a derived tag matched no service" in _LAND_SH


def test_land_still_has_a_nothing_to_deploy_path():
    """The reject half of the verdict change: a docs-only PR really is finished, and must not
    be reported as needing a human."""
    assert "nothing-to-deploy" in _LAND_SH


def test_a_secrets_rotation_is_flagged():
    """PR #695's real shape: it rotated `ruleset_drift_push_token` and touched no role at all,
    so it derived zero tags and land.sh reported `nothing-to-deploy` and exited 0. Both
    consumers -- the uptime-kuma tile and the gitops_deploy pusher cron -- kept rendering the
    old value (2026-09-01). A secret's value lives in no role's template, so changed-file tag
    scoping is structurally blind to a rotation and the note is the only signal."""
    files = [
        "ansible/secret_rotation.yml",
        "ansible/vars/secrets.yml",
        "prek.toml",
        "scripts/secrets_mgmt/secret_rotation.py",
        "scripts/secrets_mgmt/test_secret_rotation.py",
    ]
    tags, source = land_tags.derive(files, changed_files=5)
    assert (tags, source) == ([], "pr"), (
        "a rotation maps to no service tag by construction"
    )
    note = land_tags.plane_note(files)
    assert note != "", "a rotation must not read as nothing-to-deploy"
    assert "secret_rotation.py consumers" in note, (
        "the note must name the RUNNABLE resolver, like both sibling notes do, not just say a "
        "secret changed -- an operator who cannot resolve the consumers is where PR #695 was "
        "already left. `consumers ruleset_drift_push_token` prints that PR's two missing "
        "deploys, one per plane"
    )
    assert "consumer_tags()" in note and "CROSS_HOST_PUSH_TOKENS" in note, (
        "and the name-routing table behind it, whose MANUAL members each carry a written reason"
    )


def test_a_pr_without_secrets_is_clean():
    """The reject half. A rule that fired on every landing would make `needs-manual-apply` the
    normal verdict and train the operator to ignore it. The registry is the near miss worth
    pinning: `ansible/secret_rotation.yml` carries names, dates and tiers but no values, so a
    registry-only change deploys nothing and needs nothing."""
    files = [
        "ansible/secret_rotation.yml",
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
    ]
    assert land_tags.plane_note(files) == ""


def test_a_secrets_rotation_beside_a_service_is_flagged():
    """The harder silence, the same shape as the setup-plane mixed case: sonarr really deploys,
    so the verdict would otherwise read `settled`. A PR shipping secrets.yml WITH one consuming
    template still cannot show the secret's OTHER consumers, so the note must survive a
    non-empty tag list."""
    files = [
        "ansible/vars/secrets.yml",
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
    ]
    tags, _ = land_tags.derive(files, changed_files=2)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) != ""
