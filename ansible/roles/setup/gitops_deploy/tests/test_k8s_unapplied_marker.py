"""The non-paging durable record of a k8s change this deployer will never apply (#2570).

`k8s_deferred` records the ONE class of k8s deferral the tick chose for itself — a promoted
bump the broad arm ran out of budget for, plus a staging demotion — and monitor-bridge pages
on its age. The hand-edited and denylisted classes were left out of it on a measured ground:
forty of the fifty-four k8s roles are denylisted, so a page on their ordinary changes would
hold GitOps Deploy — Status red as normal operation. That rules out a signal that pages; it
does not rule out a durable record, and `k8s_unapplied` is that record.

Two properties carry the whole design, and each has a test here:

  * NOTHING PAGES ON IT. `gitops_status` never opens the file, by construction.
  * IT DISCHARGES ITSELF off the release record, so an operator's own `deploy.sh` — which the
    deployer cannot otherwise see — drops the line without anybody running a command.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_marker.py
"""

import json
import pathlib

import pytest

import deploy_defer
import deploy_release
from deploy_changes import ChangeSet
from deploy_toolbox import DeployTools

ORIGIN = "a" * 40
APPLIED = "b" * 40


def _tools(**kwargs) -> DeployTools:
    return DeployTools(discord_post=lambda _webhook, _content: True, **kwargs)


# ── the write: every deferred k8s role, on every exit that leaves the range merged ─────────
def test_a_deferred_k8s_role_is_recorded_with_the_sha_and_the_stamp(
    gitops_deploy, state_dir, settings
):
    """FLAGGED half: the marker names the service and the SHA an operator's deploy applies."""
    deploy_defer.alert_and_record_deferred(
        _tools(),
        gitops_deploy.STATE,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s={"authelia"}),
        declared_k8s={"authelia"},
    )
    ((origin, service, _at),) = [
        (e.origin, e.service, e.at) for e in gitops_deploy.STATE.k8s_unapplied_pending()
    ]
    assert (origin, service) == (ORIGIN, "authelia")


def test_a_range_with_no_k8s_role_records_nothing(gitops_deploy, state_dir, settings):
    """CLEAN half: a tasks-only push must not leave a line nobody can discharge."""
    deploy_defer.alert_and_record_deferred(
        _tools(),
        gitops_deploy.STATE,
        settings,
        ORIGIN,
        set(),
        ChangeSet(tasks={"svca"}),
    )
    assert gitops_deploy.STATE.k8s_unapplied_pending() == []
    assert not (state_dir / "k8s_unapplied").exists()


def test_a_second_deferral_of_the_same_role_keeps_the_first_seen_stamp(
    gitops_deploy, state_dir, settings
):
    """The age the banner prints, so a later range touching the role must not reset it."""
    state = gitops_deploy.STATE
    state.record_k8s_unapplied(ORIGIN, {"authelia"}, 1000.0)
    deploy_defer.alert_and_record_deferred(
        _tools(),
        state,
        settings,
        "c" * 40,
        set(),
        ChangeSet(k8s={"authelia"}),
        declared_k8s={"authelia"},
    )
    assert [(e.origin, e.at) for e in state.k8s_unapplied_pending()] == [
        (ORIGIN, 1000.0)
    ]


def test_the_two_k8s_markers_are_separate_files(gitops_deploy, state_dir, settings):
    """A class tag on a `k8s_deferred` line would read as NO pending bump in an un-redeployed
    monitor-bridge, which is why this is a second basename rather than a fourth field."""
    state = gitops_deploy.STATE
    state.record_k8s_deferred(ORIGIN, {"sonarr"}, 1000.0)
    state.record_k8s_unapplied(ORIGIN, {"authelia"}, 1000.0)
    assert [e.service for e in state.k8s_deferred_pending()] == ["sonarr"]
    assert [e.service for e in state.k8s_unapplied_pending()] == ["authelia"]


# ── the discharge: a deploy the deployer never saw still drops the line ────────────────────
@pytest.fixture
def pending(gitops_deploy, state_dir, settings):
    """A state with one pending `k8s_unapplied` line for authelia at ORIGIN."""
    gitops_deploy.STATE.record_k8s_unapplied(ORIGIN, {"authelia"}, 1000.0)
    return gitops_deploy.STATE


def _discharge(state, settings, release_commit, is_ancestor=lambda *_a: True):
    return deploy_defer.discharge_k8s_unapplied(
        _tools(release_commit=release_commit, is_ancestor=is_ancestor), state, settings
    )


def test_a_release_record_carrying_the_change_discharges_the_line(pending, settings):
    """FLAGGED half: this is what an operator's own `deploy.sh` looks like from here."""
    assert _discharge(pending, settings, lambda _svc: APPLIED) == ["authelia"]
    assert pending.k8s_unapplied_pending() == []


def test_a_release_record_predating_the_change_keeps_the_line(pending, settings):
    """CLEAN half: the service was deployed, but not at a commit carrying this change."""
    assert _discharge(pending, settings, lambda _svc: APPLIED, lambda *_a: False) == []
    assert [e.service for e in pending.k8s_unapplied_pending()] == ["authelia"]


def test_a_missing_release_record_keeps_the_line(pending, settings):
    """No evidence of a deploy is not evidence of a deploy. A kept line costs one glance; a
    dropped one loses the only record that the change was never applied."""
    assert _discharge(pending, settings, lambda _svc: None) == []
    assert [e.service for e in pending.k8s_unapplied_pending()] == ["authelia"]


# ── the release record reader ──────────────────────────────────────────────────────────────
def test_release_commit_reads_the_applied_commit(tmp_path):
    (tmp_path / "authelia.json").write_text(json.dumps({"commit": APPLIED}))
    assert deploy_release.release_commit("authelia", str(tmp_path)) == APPLIED


@pytest.mark.parametrize(
    "content", ["not json", json.dumps({}), json.dumps({"commit": ""}), None]
)
def test_an_unusable_release_record_reads_as_no_commit(tmp_path, content):
    if content is not None:
        (tmp_path / "authelia.json").write_text(content)
    assert deploy_release.release_commit("authelia", str(tmp_path)) is None


def test_the_release_dir_matches_the_manifests_role():
    """A drifted path would make every pending line undischargeable, silently.

    The deployer runs under `uv run --no-project` and cannot import `probe_lib/releases.py`,
    which mirrors the same default for the same reason, so the constant is restated and this
    is what keeps it true.
    """
    defaults = (
        pathlib.Path(__file__).resolve().parents[4]
        / "roles/k8s/manifests/defaults/main.yml"
    ).read_text()
    assert f"manifests_release_dir: {deploy_release.K8S_RELEASE_DIR}" in defaults


def test_a_service_already_paging_from_k8s_deferred_is_not_recorded_twice(
    gitops_deploy, state_dir, settings
):
    """`deploy_broad_k8s` folds a budget-deferred bump back into `cs.k8s` after recording it
    in the paging marker, so the banner would otherwise name the service twice."""
    state = gitops_deploy.STATE
    state.record_k8s_deferred(ORIGIN, {"sonarr"}, 1000.0)
    deploy_defer.alert_and_record_deferred(
        _tools(),
        state,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s={"sonarr", "authelia"}),
        declared_k8s={"sonarr", "authelia"},
    )
    assert [e.service for e in state.k8s_unapplied_pending()] == ["authelia"]
