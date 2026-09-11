"""`DeployerState` reads the same files, at the same paths, with the same three outcomes.

Fifteen marker files were read through a bare `_read_marker(path)` helper and fifteen module
constants. `deploy_io.DeployerState` wraps them. Nothing about the on-disk layout changed, so
what this module pins is that nothing about it changed:

- the derived paths still equal the literals `gitops_deploy.py` declares, one for one;
- a MISSING file, an EMPTY file and an UNREADABLE directory are still told apart the way the
  old helper told them apart — the first two read as None, the third RAISES.

That third case is the one worth writing down. `_read_marker` caught `FileNotFoundError` only,
so a state directory with the wrong mode propagated an `OSError` and the tick paged. Widening
that to a bare `except OSError` would be the `land_lib` defect the same review files separately
(finding 13): a host that is HELD would report converged, because "cannot read hold_sha" and
"there is no hold" would produce the same answer.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py
"""

import os
import pathlib

import pytest

import deploy_io

SHA = "c0ffee12" * 5


@pytest.fixture
def state(tmp_path: pathlib.Path) -> deploy_io.DeployerState:
    return deploy_io.DeployerState(tmp_path)


# ── the paths did not move ────────────────────────────────────────────────────────────────
def test_every_marker_resolves_to_the_constant_gitops_deploy_declares(gitops_deploy):
    """The two representations of the same fifteen paths, asserted equal by name.

    A named mapping rather than a count: a marker that lost its constant has to fail with its
    own name in the message, and a count would also pass if two were swapped.
    """
    live = deploy_io.DeployerState(deploy_io.STATE_DIR)
    for marker, constant in (
        ("hold", "HOLD_FILE"),
        ("hold_plane", "HOLD_PLANE_FILE"),
        ("broad_applied", "BROAD_APPLIED_FILE"),
        ("manual_plane", "MANUAL_PLANE_FILE"),
        ("last_run", "LAST_RUN"),
        ("diverged", "DIVERGED_FILE"),
        ("behind", "BEHIND_FILE"),
        ("stale_composes", "STALE_COMPOSE_FILE"),
        ("broad_alerted", "BROAD_FILE"),
        ("secrets_alerted", "SECRETS_ALERT_FILE"),
        ("tasks_alerted", "TASKS_ALERT_FILE"),
        ("meta_alerted", "META_ALERT_FILE"),
        ("k8s_alerted", "K8S_ALERT_FILE"),
        ("stale_denylist_alerted", "STALE_DENYLIST_FILE"),
        ("denylist_rendered", "DENYLIST_RENDER_FILE"),
        ("ci_alerted", "CI_ALERT_FILE"),
        ("staging_alerted", "STAGING_ALERT_FILE"),
        ("dirty_alerted", "DIRTY_ALERT_FILE"),
        ("pending_alerts", "PENDING_ALERTS_FILE"),
        ("staging_ticks", "STAGING_TICK_LEDGER"),
        ("staging_override", "STAGING_OVERRIDE_FILE"),
    ):
        assert live.path(marker) == getattr(gitops_deploy, constant), marker


def test_the_marker_table_covers_every_constant_and_no_more():
    """Non-vacuity for the loop above: it names twenty-one markers, and so must the table."""
    assert len(deploy_io.DeployerState.MARKERS) == 21


def test_an_unknown_marker_is_a_typo_not_a_new_file(state):
    with pytest.raises(KeyError):
        state.path("hold_shaa")


# ── missing vs empty vs unreadable ────────────────────────────────────────────────────────
def test_a_missing_marker_reads_as_none(state):
    assert state.read("hold") is None
    assert state.hold_sha is None


def test_an_empty_marker_reads_as_none_too(state):
    """Deliberately the same answer as missing: a torn write that left a zero-length file is a
    disarmed hold, not a hold on the SHA "". A distinction here would page on an empty file."""
    pathlib.Path(state.path("hold")).write_text("")
    assert state.read("hold") is None
    pathlib.Path(state.path("hold")).write_text("   \n")
    assert state.read("hold") is None


def test_an_unreadable_state_directory_raises_rather_than_reading_as_no_hold(state):
    """The distinction the accessors must NOT collapse.

    `read` catches FileNotFoundError and nothing else, so a permission fault propagates and the
    tick pages. Swallowing it would make a HELD host report converged — monitor-bridge gates
    GitOps Deploy — Status on `hold_sha` alone, so the tile would go green over an unapplied
    plane.
    """
    marker = pathlib.Path(state.path("hold"))
    marker.write_text(SHA)
    marker.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            state.read("hold")
    finally:
        marker.chmod(0o600)


def test_a_readable_marker_round_trips(state):
    state.write("hold", SHA)
    assert state.read("hold") == SHA
    assert pathlib.Path(state.path("hold")).read_text() == SHA


def test_writing_none_removes_the_marker_and_removing_twice_is_fine(state):
    state.write("hold", SHA)
    state.write("hold", None)
    assert not os.path.exists(state.path("hold"))
    state.write("hold", None)  # already gone: a disarm must be idempotent
    assert state.hold_sha is None


# ── the named properties ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("prop", "marker"),
    [
        ("hold_sha", "hold"),
        ("hold_plane", "hold_plane"),
        ("diverged_sha", "diverged"),
        ("behind_since", "behind"),
    ],
)
def test_each_named_property_reads_its_own_marker(state, prop, marker):
    """These four are read from outside the deployer (monitor-bridge mounts three of them), so
    a property wired to the wrong file would be a monitor reporting on the wrong fact."""
    state.write(marker, SHA)
    assert getattr(state, prop) == SHA


# ── the manual_plane marker ───────────────────────────────────────────────────────────────
# The two setup roles `initial_setup.yml` cannot apply, as the marker records them: k3s is
# applied by k3s-bringup.yml, common by no playbook at all.
K3S_LINE = ("ansible/k3s-bringup.yml", "k3s")
COMMON_LINE = ("none", "common")


def test_a_pending_role_is_recorded_as_one_parsable_line(state):
    assert state.record_manual_plane(SHA, *K3S_LINE, 1000.0) is True
    assert pathlib.Path(state.path("manual_plane")).read_text().splitlines() == [
        f"{SHA} ansible/k3s-bringup.yml k3s 1000.0"
    ]
    (entry,) = state.manual_plane_pending()
    assert entry.origin == SHA
    assert entry.playbook == "ansible/k3s-bringup.yml"
    assert entry.role == "k3s"
    assert entry.at == 1000.0


def test_a_second_role_appends_and_the_same_role_does_not(state):
    """Dedupe is by role, so a role pending since an older SHA keeps its first-seen stamp.

    Re-adding it would restart the age clock every tick and the monitor could never page.
    """
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    assert state.record_manual_plane("beef" * 10, *COMMON_LINE, 2000.0) is True
    assert state.record_manual_plane("cafe" * 10, *K3S_LINE, 3000.0) is False
    assert [(e.role, e.at) for e in state.manual_plane_pending()] == [
        ("k3s", 1000.0),
        ("common", 2000.0),
    ]


def test_clearing_one_role_leaves_the_other(state):
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.record_manual_plane(SHA, *COMMON_LINE, 2000.0)
    assert state.clear_manual_plane("k3s") is True
    assert [e.role for e in state.manual_plane_pending()] == ["common"]


def test_clearing_a_role_that_is_not_pending_changes_nothing(state):
    """The rejecting half: an operator clearing twice, or naming a role nobody recorded."""
    state.record_manual_plane(SHA, *COMMON_LINE, 2000.0)
    assert state.clear_manual_plane("k3s") is False
    assert [e.role for e in state.manual_plane_pending()] == ["common"]


def test_clearing_the_last_role_removes_the_marker_entirely(state):
    """An empty file reads as None everywhere else, so leave none behind."""
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.clear_manual_plane("k3s")
    assert state.manual_plane is None
    assert not os.path.exists(state.path("manual_plane"))


def test_an_apply_of_the_roles_own_playbook_and_tag_clears_its_line(state):
    """The deployer's own clear path: a role that becomes applyable is cleared by applying it."""
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    assert state.clear_manual_plane_applied("ansible/k3s-bringup.yml", ["k3s"]) == [
        "k3s"
    ]
    assert state.manual_plane_pending() == []


def test_an_apply_of_a_different_playbook_leaves_the_line(state):
    """The rejecting half, and the reason this is not keyed on the tag alone.

    `initial_setup.yml --tags k3s` is exactly the run that exits 0 having matched no task —
    the failure `setup_tags_for` returns nothing to avoid. It must not clear the marker.
    """
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    assert state.clear_manual_plane_applied("ansible/initial_setup.yml", ["k3s"]) == []
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


def test_a_garbled_line_is_neither_pending_nor_lost(state):
    """Fails open like `behind_since`: garbage must not page, and must not be silently dropped."""
    pathlib.Path(state.path("manual_plane")).write_text(
        f"{SHA} ansible/k3s-bringup.yml k3s not-a-number\ngarbage\n"
    )
    assert state.manual_plane_pending() == []
    state.record_manual_plane(SHA, *COMMON_LINE, 2000.0)
    assert "garbage" in pathlib.Path(state.path("manual_plane")).read_text()
    assert [e.role for e in state.manual_plane_pending()] == ["common"]


def test_the_marker_key_is_the_role_name_for_every_pending_role():
    """An operator clears by the role name the alert prints, so the two must be one word.

    The marker's third field is `setup_role_tag(role)`, and only a role
    `initial_setup.yml` does not apply can ever be written there. Both of those roles are
    tagged by their own name today. A future one that is not (the `chezmoi_setup` /
    `chezmoi` shape) would make `clear-manual-plane <role>` miss its line, so it fails here
    rather than on a host.
    """
    import deploy_changes

    roles = set(deploy_changes._SETUP_ROLES_OUTSIDE_INITIAL_SETUP)
    assert roles >= {"k3s", "common"}, roles
    for role in roles:
        assert deploy_changes.setup_role_tag(role) == role, role
