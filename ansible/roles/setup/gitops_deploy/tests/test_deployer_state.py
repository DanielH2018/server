"""`DeployerState` reads the same files, at the same paths, with the same three outcomes.

Marker files were read through a bare `_read_marker(path)` helper and a module constant each.
`deploy_io.DeployerState` wraps them, and since issue #2051 `MARKERS` is the only table of
them. Nothing about the on-disk layout changed, so what this module pins is that nothing
about it changed:

- the table is exactly the 22 named pairs below, and each resolves under the host directory;
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
# The 23 markers by name, as `MARKERS` key -> basename on disk. A frozenset of pairs rather
# than a count: a renamed basename or a swapped pair fails naming the marker, where a count
# would pass either. Add a pair here when a marker is added, and nowhere else (issue #2051 —
# these were 22 module-level constants in gitops_deploy.py with no production reader).
EXPECTED_MARKERS = frozenset(
    {
        ("hold", "hold_sha"),
        ("hold_plane", "hold_plane"),
        ("broad_applied", "broad_applied"),
        ("manual_plane", "manual_plane"),
        ("manual_plane_tags", "manual_plane_tags"),
        ("contention", "contention_since"),
        ("last_run", "last_run"),
        ("diverged", "diverged_sha"),
        ("behind", "behind_since"),
        ("stale_composes", "stale_composes_alerted"),
        ("broad_alerted", "broad_alerted_sha"),
        ("secrets_alerted", "secrets_alerted_sha"),
        ("tasks_alerted", "tasks_alerted_sha"),
        ("meta_alerted", "meta_alerted_sha"),
        ("k8s_alerted", "k8s_alerted_sha"),
        ("stale_denylist_alerted", "stale_denylist_alerted_sha"),
        ("denylist_rendered", "denylist_rendered_sha"),
        ("ci_alerted", "ci_alerted_sha"),
        ("staging_alerted", "staging_alerted_sha"),
        ("dirty_alerted", "dirty_alerted_date"),
        ("pending_alerts", "pending_alerts.json"),
        ("staging_ticks", "staging-ticks.jsonl"),
        ("staging_override", "staging_gate_override"),
    }
)


def test_the_marker_table_is_exactly_the_named_census():
    assert frozenset(deploy_io.DeployerState.MARKERS.items()) == EXPECTED_MARKERS


def test_every_marker_resolves_under_the_host_state_directory():
    live = deploy_io.DeployerState(deploy_io.STATE_DIR)
    for marker, basename in EXPECTED_MARKERS:
        assert live.path(marker) == f"{deploy_io.STATE_DIR}/{basename}", marker


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


# ── the manual_plane_tags sidecar (#2307) ─────────────────────────────────────────────────


def test_the_narrow_tags_of_a_pending_role_round_trip(state):
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    assert state.manual_plane_tags_pending() == {"k3s": frozenset({"kubeconfig"})}


def test_a_second_range_on_the_same_role_unions_its_tags(state):
    """Both changes are merged and unapplied, so both tags have to run."""
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    state.record_manual_plane_tags("k3s", frozenset({"coredns"}), line_predates=True)
    assert state.manual_plane_tags_pending() == {
        "k3s": frozenset({"coredns", "kubeconfig"})
    }


def test_a_refusal_widens_a_role_that_was_already_narrowed(state):
    """The rejecting half, and the direction that must not be reversible.

    A range nothing could narrow needs the whole role. A narrow tag left beside it would read
    like the complete answer while describing half the work, so the refusal absorbs the pair —
    in both arrival orders, since the tick order is not ours to choose.
    """
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    state.record_manual_plane_tags("k3s", None, line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}
    state.record_manual_plane_tags("k3s", frozenset({"coredns"}), line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}, (
        "a refusal already recorded is not narrowed away by a later range"
    )


def test_a_line_that_predates_the_range_with_no_row_is_flagged_as_the_whole_role(state):
    """Unknown joined with anything is the whole role (#2307 review, finding 2).

    A line written by the deployer from before the sidecar has no row, so what its range
    needed is unknown. Narrowing it to the NEXT range's answer would leave the first change
    unapplied behind a clear command.
    """
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.record_manual_plane_tags("k3s", frozenset({"kubeconfig"}), line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}


def test_clearing_a_role_takes_its_narrow_tags_with_it(state):
    """A row outliving its line is a row nothing can clear.

    Every reader looks the tags up by the `manual_plane` line the role no longer has, so a
    stale row would be handed to the next range recording the same role — naming a tag that
    range never touched.
    """
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    state.record_manual_plane(SHA, *COMMON_LINE, 2000.0)
    state.record_manual_plane_tags("common", frozenset({"resolv"}), line_predates=False)
    assert state.clear_manual_plane("k3s") is True
    assert state.manual_plane_tags_pending() == {"common": frozenset({"resolv"})}
    state.clear_manual_plane("common")
    assert not os.path.exists(state.path("manual_plane_tags"))


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


# ── the contention_since marker (issue #1847) ─────────────────────────────────────────────
def test_a_contention_streak_keeps_its_first_seen_and_counts(state):
    first = state.record_contention(SHA, "sonarr", 1000.0)
    assert (first.first_seen, first.last_seen, first.count) == (1000.0, 1000.0, 1)
    second = state.record_contention(SHA, "sonarr", 1900.0)
    assert (second.first_seen, second.last_seen, second.count) == (1000.0, 1900.0, 2)
    assert state.contention_pending() == second
    assert state.read("contention") == f"{SHA} sonarr 1000.0 1900.0 2"


def test_a_contention_marker_with_no_lock_name_keeps_five_fields(state):
    """`ServiceLockBusy` raised with a message alone has no lock; the readers split on
    whitespace and a four-field line would read as garbage."""
    assert state.record_contention(SHA, "", 1.0).lock == "unknown"
    assert state.contention_pending() is not None


def test_a_garbled_contention_marker_reads_as_no_streak_and_is_overwritten(state):
    """Fails open like `behind_since`: garbage must not page. A new defer starts over."""
    state.write("contention", f"{SHA} sonarr not-a-stamp 2 1")
    assert state.contention_pending() is None
    assert state.record_contention(SHA, "sonarr", 5.0).count == 1


def test_a_tick_that_ended_another_way_clears_the_streak(state):
    """The reverse of `record_contention`, keyed on `last_seen` against the tick's start."""
    state.record_contention(SHA, "sonarr", 1000.0)
    assert not state.clear_contention_unless_touched_since(tick_started=900.0), (
        "the tick that wrote the marker must not clear it"
    )
    assert state.clear_contention_unless_touched_since(tick_started=1500.0)
    assert state.contention_pending() is None
    assert not state.clear_contention(), "clearing twice is a no-op, not an error"


def test_an_operators_clear_removes_the_marker(state):
    state.record_contention(SHA, "all", 1.0)
    assert state.clear_contention()
    assert state.read("contention") is None
