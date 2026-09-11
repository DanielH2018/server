"""Staleness for `probe.py releases` -- which role-path changes invalidate a release stamp.

Split out of `test_probe_releases.py`, which covers the reader itself (flags, per-service
lookup, loading, missing_services). Everything here drives a throwaway git repo, because
staleness is a question about history rather than about a record's fields. Shared fixtures are
in `_release_fixtures.py`.

Every rule is a `..._is_clean` / `..._is_flagged` pair, and the #1672 narrowing carries a third
assertion that it did not widen -- a service's own `tasks/` and the renderer's `tasks/` must
still count stale.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_stale.py
"""

from diagnostics.probe_lib import releases as pr

from _release_fixtures import _commit, _init_repo, _record, _set_origin_master


def test_named_role_is_stale_after_its_own_role_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"},
        "sonarr v2",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)], repo_root=repo, shared_roles=set()
    )
    assert "sonarr" in stale
    assert "deployment.yaml.j2" in stale["sonarr"]


def test_named_role_is_clean_when_its_record_is_the_tip(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"},
        "sonarr v2",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=tip)], repo_root=repo, shared_roles=set()
    )
    assert stale == {}


def test_shared_role_change_marks_the_consuming_service_stale(tmp_path):
    """Without widening onto the shared role, a `manifests`-only change reads clean for every
    service -- the exact false-GREEN issue #947 names."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n",
            "ansible/roles/k8s/manifests/tasks/main.yml": "v1\n",
        },
        "baseline",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/manifests/tasks/main.yml": "v2\n"},
        "shared role changes, sonarr's own role does not",
    )
    _set_origin_master(repo, tip)
    record = [_record("sonarr", commit=base)]

    without_widening = pr.compute_stale(record, repo_root=repo, shared_roles=set())
    assert without_widening == {}, (
        "reproduces the false-GREEN this feature exists to close"
    )

    with_widening = pr.compute_stale(record, repo_root=repo, shared_roles={"manifests"})
    assert "sonarr" in with_widening
    assert "manifests/tasks/main.yml" in with_widening["sonarr"]


def test_docs_and_test_only_changes_do_not_mark_a_service_stale(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/README.md": "docs\n",
            "ansible/roles/k8s/sonarr/tests/test_something.py": "assert True\n",
        },
        "docs and a role-local test, neither deployed",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)], repo_root=repo, shared_roles=set()
    )
    assert stale == {}


def test_unresolvable_commit_is_stale_not_skipped(tmp_path):
    """A commit this checkout has never seen (a pruned branch, a shallow clone) must read as
    stale, not silently pass -- a record naming an unknown commit is not evidence of anything."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit="f" * 40)], repo_root=repo, shared_roles=set()
    )
    assert stale.get("sonarr") == "commit unknown to this checkout"


# ── a shared role's tasks/ is deploy-time, not applied bytes (#1672) ─────────────────────────


def test_a_shared_roles_tasks_change_does_not_mark_every_service_stale(tmp_path):
    """The live incident: `volume-claim`'s staging-directory move (0b86a7d7).

    That commit touched one non-doc, non-test file -- `volume-claim/tasks/claim.yml` -- and
    marked all 53 services stale, parking `Release Staleness Drift` DOWN with no deploy tag able
    to clear it. `volume-claim` is in the census for its `pvc.yaml.j2`, but its `tasks/` decides
    how the deploy runs, never what it applies.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n",
            "ansible/roles/k8s/volume-claim/tasks/claim.yml": "v1\n",
            "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2": "v1\n",
        },
        "baseline",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/volume-claim/tasks/claim.yml": "v2\n"},
        "stage claims outside the pruned directory",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
    )
    assert stale == {}


def test_a_shared_roles_template_change_still_marks_every_service_stale(tmp_path):
    """The narrowing must not blind the check to the bytes that role really does supply."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n",
            "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2": "v1\n",
        },
        "baseline",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2": "v2\n"},
        "the claim's own bytes move",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
    )
    assert "sonarr" in stale
    assert "volume-claim/templates/pvc.yaml.j2" in stale["sonarr"]


def test_a_shared_roles_defaults_change_still_marks_every_service_stale(tmp_path):
    """`defaults/` stays in on purpose.

    `volume-claim/defaults/main.yml` holds `volume_claim_size` and
    `volume_claim_storage_class`, both read by that role's `pvc.yaml.j2` -- so a change there
    moves the applied PVC, unlike a `tasks/` change.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n",
            "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2": "v1\n",
            "ansible/roles/k8s/volume-claim/defaults/main.yml": "volume_claim_size: 1Gi\n",
        },
        "baseline",
    )
    tip = _commit(
        repo,
        {
            "ansible/roles/k8s/volume-claim/defaults/main.yml": "volume_claim_size: 2Gi\n"
        },
        "the default the template reads moves",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
    )
    assert "sonarr" in stale
    assert "volume-claim/defaults/main.yml" in stale["sonarr"]


def test_a_services_own_tasks_change_still_marks_it_stale(tmp_path):
    """The narrowing is scoped to SHARED roles.

    A service's own `tasks/main.yml` names its `manifests_files` -- the list `k8s/manifests`
    stages -- so a change there does move its applied bytes. Excluding it would be a
    false-GREEN of the #947 class the check exists to catch.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo, {"ansible/roles/k8s/sonarr/tasks/main.yml": "v1\n"}, "baseline"
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/tasks/main.yml": "v2\n"},
        "sonarr adds a manifest to its file list",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
    )
    assert "sonarr" in stale
    assert "sonarr/tasks/main.yml" in stale["sonarr"]


def test_deploy_time_shared_roles_spares_the_manifest_renderer():
    """`manifests` ships no templates -- its `tasks/` IS the render, so it must stay in.

    Named members rather than a count: a role that moves or is renamed fails here by name.
    """
    census = pr.manifest_affecting_shared_roles()
    deploy_time = pr._deploy_time_shared_roles(census)
    assert pr.MANIFEST_RENDERER not in deploy_time
    assert deploy_time == {
        "arr-notification",
        "game-stats-lib",
        "image-builder",
        "volume-claim",
    }


def test_is_real_change_is_clean_for_a_shared_roles_tasks_file():
    assert (
        pr._is_real_change(
            "ansible/roles/k8s/volume-claim/tasks/claim.yml",
            frozenset({"volume-claim"}),
        )
        is False
    )


def test_is_real_change_is_flagged_for_the_paths_the_narrowing_must_not_touch():
    """Four paths a too-wide exclusion would swallow, each still a real change."""
    deploy_time = frozenset({"volume-claim"})
    for path in (
        "ansible/roles/k8s/volume-claim/templates/pvc.yaml.j2",
        "ansible/roles/k8s/volume-claim/defaults/main.yml",
        "ansible/roles/k8s/sonarr/tasks/main.yml",
        "ansible/roles/k8s/manifests/tasks/release_stamp.yml",
    ):
        assert pr._is_real_change(path, deploy_time) is True, path
