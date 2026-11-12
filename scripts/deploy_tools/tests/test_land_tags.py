#!/usr/bin/env python3
"""Tests for land_tags.py -- deriving deploy tags from a merged PR's own file list.

The failure this guards is a SILENTLY NARROWED tag list. `gh pr view --json files`
paginates at 100, so a large PR returns a truncated list that looks complete, and the
deploy then covers a subset of what merged while every check reads green. That is the
"a derivation can narrow the list it replaces" class, and it is invisible from the
passing side -- so every rule here has a reject half.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags.py
"""

import pytest


from deploy_tools.land_lib import land_tags

# A fixture, not live inventory. These tests pin the DERIVATION, and reading containers_list
# would make them fail whenever a service is retired. The live set is checked once,
# separately, by test_the_shared_roles_are_still_undeclared.
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
    """100 returned against 137 changed means the list is truncated.

    The tags returned alongside `fallback` are empty on purpose -- a partial list is worse than
    none, because it looks like an answer.
    """
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


def test_missing_changed_files_count_falls_back_rather_than_guessing():
    """`gh` omitting changedFiles must not be read as agreement.

    land_tags is handed -1 in that case, which can never equal a real length.
    """
    tags, source = land_tags.derive(["docs/index.md"], changed_files=-1)
    assert source == "fallback"
    assert tags == []


def test_a_build_role_pulls_in_the_workload_that_runs_its_image():
    """A build role must pull in the workload that runs its image.

    The n8n role builds its own images, so the Dockerfiles derive the workload's tag directly.
    """
    files = [
        "ansible/roles/k8s/n8n/templates/Dockerfile.j2",
        "ansible/roles/k8s/n8n/templates/Dockerfile-runners.j2",
    ]
    tags, source = land_tags.derive(files, changed_files=2)
    assert source == "pr"
    assert tags == ["n8n"]


def test_an_ordinary_role_is_not_widened():
    """The reject half. A deriver that widened everything would pass the test above."""
    tags, source = land_tags.derive(
        ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"], changed_files=1
    )
    assert source == "pr"
    assert tags == ["sonarr"]


_PR_617_FILES = [
    "ansible/roles/k8s/artifacts/defaults/main.yml",
    "ansible/roles/k8s/autofix-bridge/defaults/main.yml",
    "ansible/roles/k8s/docs/defaults/main.yml",
    "ansible/roles/k8s/headlamp/defaults/main.yml",
    "ansible/roles/k8s/home-assistant/defaults/main.yml",
    "ansible/roles/k8s/homepage/defaults/main.yml",
    "ansible/roles/k8s/jellyfin/defaults/main.yml",
    "ansible/roles/k8s/karakeep/defaults/main.yml",
    "ansible/roles/k8s/volume-snapshot/defaults/main.yml",
    "ansible/roles/k8s/volume-snapshot/tasks/main.yml",
    "ansible/roles/k8s/volume-snapshot/tasks/release_stamp.yml",
    "ansible/roles/k8s/media-volume/defaults/main.yml",
    "ansible/roles/k8s/monitor-bridge/defaults/main.yml",
    "ansible/roles/k8s/n8n/defaults/main.yml",
    "ansible/roles/k8s/netpol-baseline/defaults/main.yml",
    "ansible/roles/k8s/peanut/defaults/main.yml",
    "ansible/roles/k8s/prowlarr/defaults/main.yml",
    "ansible/roles/k8s/qbittorrent/defaults/main.yml",
    "ansible/roles/k8s/registry/defaults/main.yml",
    "ansible/roles/k8s/scrutiny/defaults/main.yml",
    "ansible/roles/k8s/sonarr/defaults/main.yml",
    "ansible/roles/k8s/terraria-stats/defaults/main.yml",
    "ansible/roles/k8s/traefik/defaults/main.yml",
    "ansible/roles/k8s/valheim-stats/defaults/main.yml",
    "ansible/roles/k8s/zigbee2mqtt/defaults/main.yml",
    "hosts/components/k3s-server/defaults/main.yml",
    "ansible/tests/k8s/test_base_images_digest_pinned.py",
    "docs/claude-tooling.md",
    "scripts/diagnostics/probe.py",
    "scripts/diagnostics/probe_lib/releases.py",
    "scripts/diagnostics/test_probe_releases.py",
]


def test_pr_617_derives_its_services_and_not_the_shared_roles():
    """The measured failure.

    `manifests` and `volume-claim` have no containers_list entry, so including them refused the 22
    valid services beside them -- land.sh printed nothing-to-deploy and 22 digest pins sat
    undeployed.
    """
    tags, source = land_tags.derive(_PR_617_FILES, changed_files=len(_PR_617_FILES))
    assert source == "pr"
    assert "manifests" not in tags
    assert "volume-claim" not in tags
    assert len(tags) == 22
    assert {"sonarr", "jellyfin", "traefik", "n8n"} <= set(tags)


def test_a_shared_role_with_callers_derives_no_tag_of_its_own():
    files = ["ansible/roles/k8s/volume-snapshot/tasks/main.yml"]
    tags, source = land_tags.derive(files, changed_files=1)
    assert (tags, source) == ([], "pr")
    assert land_tags.plane_note(files) == ""


def test_an_ordinary_service_role_is_neither_dropped_nor_reported():
    """The reject half of both rules above.

    A splitter that called every role shared would derive no tags at all and report a full deploy on
    every landing.
    """
    files = ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"]
    tags, _ = land_tags.derive(files, changed_files=1)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) == ""


def test_the_shared_roles_are_still_undeclared(monkeypatch):
    """The one test that reads live inventory.

    The split is only correct while these names really have no containers_list entry -- give one an
    entry and it becomes an ordinary deployable role, and this test is where that gets noticed.
    """
    monkeypatch.undo()
    declared = land_tags.declared_tags()
    assert "manifests" not in declared
    assert "volume-claim" not in declared
    assert "sonarr" in declared, (
        "the reject half: a lookup returning nothing would pass"
    )


def test_a_secrets_rotation_is_flagged():
    """A secrets rotation touching no role derives zero tags.

    It rotated `ruleset_drift_push_token` and touched no role at all, so land.sh reported
    `nothing-to-deploy` and exited 0. Both consumers -- the uptime-kuma tile and the gitops_deploy
    pusher cron -- kept rendering the old value. A secret's value lives in no role's
    template, so changed-file tag scoping is structurally blind to a rotation and the note is the
    only signal.
    """
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


def test_a_pr_without_secrets_is_clean():
    """The reject half.

    A rule that fired on every landing would make `needs-manual-apply` the normal verdict and train
    the operator to ignore it. The registry is the near miss worth pinning:
    `ansible/secret_rotation.yml` carries names, dates and tiers but no values, so a registry-only
    change deploys nothing and needs nothing.
    """
    files = [
        "ansible/secret_rotation.yml",
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
    ]
    assert land_tags.plane_note(files) == ""


def test_a_secrets_rotation_beside_a_service_is_flagged():
    """The harder silence, the same shape as the setup-plane mixed case.

    sonarr really deploys, so the verdict would otherwise read `settled`. A PR shipping secrets.yml
    WITH one consuming template still cannot show the secret's OTHER consumers, so the note must
    survive a non-empty tag list.
    """
    files = [
        "ansible/vars/secrets.yml",
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
    ]
    tags, _ = land_tags.derive(files, changed_files=2)
    assert tags == ["sonarr"]
    assert land_tags.plane_note(files) != ""


def test_a_check_table_change_lands_with_its_own_role_alone():
    tags, source = land_tags.derive(
        ["ansible/roles/k8s/monitor-bridge/files/check_table.py"], changed_files=1
    )
    assert source == "pr"
    assert tags == ["monitor-bridge"]


# --- a k8s role's defaults whose change is comments only -------------------------
#
# A comment-only edit to a k8s role's `defaults/` renders the same manifests (#4460), so the
# landing derives no tag for it and gates no workload that did not change.

_SONARR_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"


def _show(texts):
    return lambda ref, path: texts[ref]


def test_a_comment_only_defaults_edit_is_quiet():
    show = _show({"old": "a: 1\n# one\n", "new": "a: 1\n# two\n"})
    quiet = land_tags.comment_only_k8s_defaults([_SONARR_DEFAULTS], "old", "new", show)
    assert quiet == {_SONARR_DEFAULTS}
    assert land_tags.derive([_SONARR_DEFAULTS], 1, quiet=quiet).tags == []


def test_a_real_defaults_edit_is_flagged():
    show = _show({"old": "a: 1\n", "new": "a: 2\n"})
    assert (
        land_tags.comment_only_k8s_defaults([_SONARR_DEFAULTS], "old", "new", show)
        == set()
    )
    assert land_tags.derive([_SONARR_DEFAULTS], 1).tags == ["sonarr"]


def test_a_comment_line_inside_a_block_scalar_is_content():
    show = _show({"old": "a: |\n  # x\n", "new": "a: |\n  # y\n"})
    assert (
        land_tags.comment_only_k8s_defaults([_SONARR_DEFAULTS], "old", "new", show)
        == set()
    )


def test_an_unusable_range_reads_only_docs_as_quiet():
    """No range, or a malformed one, keeps every defaults path loud."""
    files = [_SONARR_DEFAULTS, "ansible/roles/k8s/sonarr/CLAUDE.md"]
    for range_ in ("", "..", "abc", "..def", "ghi.."):
        assert land_tags.quiet_paths(files, range_) == {files[1]}, range_


def test_a_range_narrower_than_the_file_list_reads_nothing_as_quiet(monkeypatch):
    """A path changed OUTSIDE the range reads as identical on both sides, so it would come
    back quiet and its tag would be skipped. The accept half is the test below."""
    monkeypatch.setattr(land_tags, "git_stdout", lambda *a, **k: "a.yml")
    monkeypatch.setattr(
        land_tags, "comment_only_k8s_defaults", lambda paths, old, new: set(paths)
    )
    assert land_tags.quiet_paths(["a.yml", "b.yml"], "old..new") == set()


def test_a_range_covering_the_file_list_is_read(monkeypatch):
    monkeypatch.setattr(land_tags, "git_stdout", lambda *a, **k: "a.yml\nb.yml")
    monkeypatch.setattr(
        land_tags, "comment_only_k8s_defaults", lambda paths, old, new: {"a.yml"}
    )
    assert land_tags.quiet_paths(["a.yml", "b.yml"], "old..new") == {"a.yml"}
