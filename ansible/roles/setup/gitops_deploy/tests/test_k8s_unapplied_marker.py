"""The non-paging durable record of a k8s change this deployer will never apply.

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
from gitops_ledger import OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED

ORIGIN = "a" * 40
APPLIED = "b" * 40
LATER = "c" * 40


def _tools(**kwargs) -> DeployTools:
    kwargs.setdefault("digest_provable", lambda _repo, _roles: set())
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
        (e.origin, e.service, e.at)
        for e in gitops_deploy.STATE.owed_pending(OWED_K8S_UNAPPLIED)
    ]
    assert (origin, service) == (ORIGIN, "authelia")


def test_each_line_names_the_commit_that_changed_its_service(
    gitops_deploy, state_dir, settings
):
    """sonarr changed at APPLIED, below a later tip. Its landing's record names APPLIED,
    so a line at the tip would never discharge. authelia has no attribution and keeps the tip."""
    deploy_defer.alert_and_record_deferred(
        _tools(),
        gitops_deploy.STATE,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s={"sonarr", "authelia"}, k8s_origins={"sonarr": APPLIED}),
        declared_k8s={"sonarr", "authelia"},
    )
    assert sorted(
        (e.service, e.origin)
        for e in gitops_deploy.STATE.owed_pending(OWED_K8S_UNAPPLIED)
    ) == [("authelia", ORIGIN), ("sonarr", APPLIED)]


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
    assert gitops_deploy.STATE.owed_pending(OWED_K8S_UNAPPLIED) == []
    assert not (state_dir / "owed.jsonl").exists()


def test_a_second_deferral_of_the_same_role_moves_the_origin_and_keeps_the_stamp(
    gitops_deploy, state_dir, settings
):
    """The line has to name the NEWEST unapplied change, or it discharges past it.

    The stamp is the age the banner prints, and it dates the oldest unapplied change, so a
    later range touching the role must not reset it.
    """
    state = gitops_deploy.STATE
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"authelia"}, 1000.0)
    deploy_defer.alert_and_record_deferred(
        _tools(),
        state,
        settings,
        LATER,
        set(),
        ChangeSet(k8s={"authelia"}),
        declared_k8s={"authelia"},
    )
    assert [(e.origin, e.at) for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == [
        (LATER, 1000.0)
    ]


def test_a_first_deferral_of_a_role_appends_its_line(
    gitops_deploy, state_dir, settings
):
    """The other half of the advance: a service the marker does not list gets a NEW line,
    stamped now, and is what `record_k8s_unapplied` returns — `deploy_defer.unrecord` clears
    exactly that, so an advanced line must stay out of it."""
    state = gitops_deploy.STATE
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"authelia"}, 1000.0)
    assert state.record_owed(
        OWED_K8S_UNAPPLIED, LATER, {"authelia", "sonarr"}, 2000.0
    ) == ["sonarr"]
    assert sorted(
        (e.origin, e.service, e.at) for e in state.owed_pending(OWED_K8S_UNAPPLIED)
    ) == [
        (LATER, "authelia", 1000.0),
        (LATER, "sonarr", 2000.0),
    ]


def _line(service: str, origin: str = ORIGIN, **fields) -> str:
    """One `owed` ledger line for a `k8s_unapplied` change, with `fields` merged over it."""
    return json.dumps(
        {"class": "k8s_unapplied", "subject": service, "origin": origin, "at": 1000}
        | fields,
        sort_keys=True,
    )


def test_a_torn_line_survives_an_origin_advance(gitops_deploy, state_dir, settings):
    """A line no parser can read is carried through untouched: the rewrite walks the RAW
    lines, which the parsed entries are not aligned with once one of them is skipped."""
    state = gitops_deploy.STATE
    state.write("owed", f"garbled\n{_line('authelia')}")
    state.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0)
    assert state.read("owed").splitlines() == ["garbled", _line("authelia", LATER)]


def test_a_key_this_writer_does_not_know_survives_an_origin_advance(
    gitops_deploy, state_dir, settings
):
    """The ledger's whole point (#3392): a key a NEWER writer added is neither a reason to
    skip the line nor something an older writer strips on its way through."""
    state = gitops_deploy.STATE
    state.write("owed", _line("authelia", future=["kept"]))
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]
    state.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0)
    assert json.loads(state.read("owed"))["future"] == ["kept"]


@pytest.mark.parametrize(
    "torn",
    [
        json.dumps({"class": "k8s_unapplied", "subject": "authelia", "origin": ORIGIN}),
        _line("authelia", at="not-a-stamp"),
    ],
    ids=["no-stamp-at-all", "a-stamp-no-parser-reads"],
)
def test_a_torn_line_naming_a_service_is_repaired_rather_than_duplicated(
    torn, gitops_deploy, state_dir, settings
):
    """A torn line the record CAN attribute is rewritten in place.

    The service is absent from `parse_owed`'s entries, so a writer reading only those appends
    a second line beside the torn one — and the clear and the discharge would then carry the
    torn one forever. One line, readable, is the only end state that clears.
    """
    state = gitops_deploy.STATE
    state.write("owed", torn)
    assert state.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0) == [], (
        "a line predating the tick must stay out of what `unrecord` clears"
    )
    assert [(e.origin, e.service) for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == [
        (LATER, "authelia")
    ]
    assert state.clear_owed(OWED_K8S_UNAPPLIED, {"authelia"}) == ["authelia"]
    assert state.read("owed") is None


def test_a_repaired_line_keeps_a_stamp_that_reads_as_one(
    gitops_deploy, state_dir, settings
):
    """A torn line's stamp is the age a reader dates the change from, so the repair keeps it."""
    state = gitops_deploy.STATE
    torn = {"class": "k8s_unapplied", "subject": "authelia", "origin": 7, "at": 1000}
    state.write("owed", json.dumps(torn))
    state.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0)
    assert [e.at for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == [1000.0]


def test_a_torn_line_beside_a_readable_one_is_dropped(
    gitops_deploy, state_dir, settings
):
    """Repairing here would duplicate what the readable line already says."""
    state = gitops_deploy.STATE
    state.write("owed", f"{_line('authelia', at=None)}\n{_line('authelia')}")
    state.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0)
    assert state.read("owed").splitlines() == [_line("authelia", LATER)]


def test_the_two_k8s_markers_are_separate_files(gitops_deploy, state_dir, settings):
    """A class tag on a `k8s_deferred` line would read as NO pending bump in an un-redeployed
    monitor-bridge, which is why `k8s_unapplied` lives in the `owed` ledger instead."""
    state = gitops_deploy.STATE
    state.record_owed(OWED_K8S_DEFERRED, ORIGIN, {"sonarr"}, 1000.0)
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"authelia"}, 1000.0)
    assert [e.service for e in state.owed_pending(OWED_K8S_DEFERRED)] == ["sonarr"]
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]


# ── the discharge: a deploy the deployer never saw still drops the line ────────────────────
@pytest.fixture
def pending(gitops_deploy, state_dir, settings):
    """A state with one pending `k8s_unapplied` line for authelia at ORIGIN."""
    gitops_deploy.STATE.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"authelia"}, 1000.0)
    return gitops_deploy.STATE


def _discharge(state, settings, release_commit, is_ancestor=lambda *_a: True):
    return deploy_defer.discharge_k8s_unapplied(
        _tools(release_commit=release_commit, is_ancestor=is_ancestor), state, settings
    )


def test_a_release_record_carrying_the_change_discharges_the_line(pending, settings):
    """FLAGGED half: this is what an operator's own `deploy.sh` looks like from here."""
    assert _discharge(pending, settings, lambda _svc: APPLIED) == ["authelia"]
    assert pending.owed_pending(OWED_K8S_UNAPPLIED) == []


def test_a_release_record_predating_the_change_keeps_the_line(pending, settings):
    """CLEAN half: the service was deployed, but not at a commit carrying this change."""
    assert _discharge(pending, settings, lambda _svc: APPLIED, lambda *_a: False) == []
    assert [e.service for e in pending.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]


def test_a_deploy_between_two_changes_does_not_discharge_the_second(pending, settings):
    """A line stuck at the OLDEST origin discharges past a change merged after it.

    Change A merged at ORIGIN, change B at LATER, and a deploy in between stamped APPLIED,
    which descends from ORIGIN and not from LATER. With the line advanced to LATER that
    deploy proves nothing about B, and the line stays.
    """
    pending.record_owed(OWED_K8S_UNAPPLIED, LATER, {"authelia"}, 2000.0)
    assert (
        _discharge(
            pending,
            settings,
            lambda _svc: APPLIED,
            lambda _repo, origin, _commit: origin == ORIGIN,
        )
        == []
    )
    assert [
        (e.origin, e.service) for e in pending.owed_pending(OWED_K8S_UNAPPLIED)
    ] == [(LATER, "authelia")]


def test_a_missing_release_record_keeps_the_line(pending, settings):
    """No evidence of a deploy is not evidence of a deploy. A kept line costs one glance; a
    dropped one loses the only record that the change was never applied."""
    assert _discharge(pending, settings, lambda _svc: None) == []
    assert [e.service for e in pending.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]


# ── a shared role has no record of its own, so its callers' records stand in ───────────
@pytest.fixture
def shared_pending(gitops_deploy, state_dir, settings):
    """A pending `k8s_unapplied` line for the shared role `game-stats-lib` at ORIGIN."""
    gitops_deploy.STATE.record_owed(
        OWED_K8S_UNAPPLIED, ORIGIN, {"game-stats-lib"}, 1000.0
    )
    return gitops_deploy.STATE


def _discharge_shared(
    state, settings, behind=(), shared_role_callers=None, render_proof=None
):
    tools = _tools(
        release_commit=lambda svc: None if svc in SHARED else APPLIED + svc,
        is_ancestor=lambda _repo, _origin, commit: commit[40:] not in behind,
        shared_role_callers=shared_role_callers
        or (
            lambda _repo, roles: {r: {"terraria-stats", "valheim-stats"} for r in roles}
        ),
        render_proof=render_proof or (lambda _svc: None),
    )
    return deploy_defer.discharge_k8s_unapplied(tools, state, settings)


SHARED = {"game-stats-lib", "manifests"}


def test_a_shared_role_whose_callers_all_carry_the_change_is_discharged(
    shared_pending, settings
):
    """FLAGGED half: a full deploy that carries the change at every caller discharges the line."""
    assert _discharge_shared(shared_pending, settings) == ["game-stats-lib"]
    assert shared_pending.owed_pending(OWED_K8S_UNAPPLIED) == []


def test_a_shared_role_with_one_caller_behind_is_kept(shared_pending, settings):
    """CLEAN half: one caller's record predates the change, so it is not applied there."""
    assert _discharge_shared(shared_pending, settings, behind={"valheim-stats"}) == []
    assert [e.service for e in shared_pending.owed_pending(OWED_K8S_UNAPPLIED)] == [
        "game-stats-lib"
    ]


@pytest.mark.parametrize(
    "shared_role_callers",
    [
        pytest.param(lambda _repo, roles: {r: set() for r in roles}, id="no-callers"),
        pytest.param(
            lambda _repo, _roles: (_ for _ in ()).throw(ValueError("bad json")),
            id="derivation-fails",
        ),
    ],
)
def test_a_shared_role_with_no_derivable_caller_is_kept(
    shared_pending, settings, shared_role_callers
):
    """An empty caller set must not read as vacuously covered, nor a crash as evidence."""
    assert _discharge_shared(shared_pending, settings, (), shared_role_callers) == []
    assert [e.service for e in shared_pending.owed_pending(OWED_K8S_UNAPPLIED)] == [
        "game-stats-lib"
    ]


# ── a render whose digests match stands in for a caller's deploy, for `manifests` ───────
def _render_proved(svc):
    """valheim-stats' render, at a commit descending from the change, matches its digests."""
    return LATER + "render" if svc == "valheim-stats" else None


def test_a_manifests_line_discharges_on_a_matching_render(
    gitops_deploy, state_dir, settings
):
    """FLAGGED half: valheim-stats was never redeployed, but its bytes did not move."""
    gitops_deploy.STATE.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"manifests"}, 1000.0)
    assert _discharge_shared(
        gitops_deploy.STATE, settings, {"valheim-stats"}, None, _render_proved
    ) == ["manifests"]


def test_a_role_acting_outside_the_digest_ignores_a_matching_render(
    shared_pending, settings
):
    """CLEAN half: `game-stats-lib` is not in `DIGEST_PROVABLE_ROLES`, so a render proves
    nothing for it and the caller whose record predates the change keeps the line."""
    assert (
        _discharge_shared(
            shared_pending, settings, {"valheim-stats"}, None, _render_proved
        )
        == []
    )
    assert "game-stats-lib" not in deploy_defer.DIGEST_PROVABLE_ROLES


# ── a service's own line takes the render proof when its role is digest-provable ─────────
def _discharge_own(state, settings, digest_provable):
    """authelia's record predates the line; its render at LATER matches the applied bytes."""
    return deploy_defer.discharge_k8s_unapplied(
        _tools(
            release_commit=lambda _svc: APPLIED,
            is_ancestor=lambda _repo, _origin, commit: commit == LATER,
            render_proof=lambda _svc: LATER,
            digest_provable=digest_provable,
        ),
        state,
        settings,
    )


def test_an_own_line_discharges_on_a_matching_render_when_provable(pending, settings):
    """FLAGGED half: a comment-only template change, never redeployed, bytes unmoved."""
    assert _discharge_own(pending, settings, lambda _r, roles: set(roles)) == [
        "authelia"
    ]


@pytest.mark.parametrize(
    "digest_provable",
    [
        pytest.param(lambda _r, _roles: set(), id="acts-outside-the-digest"),
        pytest.param(
            lambda _r, _roles: (_ for _ in ()).throw(ValueError("bad json")),
            id="derivation-fails",
        ),
    ],
)
def test_an_own_line_ignores_a_matching_render_unless_provable(
    pending, settings, digest_provable
):
    """CLEAN half: a role that writes a host file or calls an API is not proved by bytes."""
    assert _discharge_own(pending, settings, digest_provable) == []
    assert [e.service for e in pending.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]


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
    state.record_owed(OWED_K8S_DEFERRED, ORIGIN, {"sonarr"}, 1000.0)
    deploy_defer.alert_and_record_deferred(
        _tools(),
        state,
        settings,
        ORIGIN,
        set(),
        ChangeSet(k8s={"sonarr", "authelia"}),
        declared_k8s={"sonarr", "authelia"},
    )
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]
