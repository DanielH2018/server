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
        ("ci_alerted", "CI_ALERT_FILE"),
        ("staging_alerted", "STAGING_ALERT_FILE"),
        ("dirty_alerted", "DIRTY_ALERT_FILE"),
    ):
        assert live.path(marker) == getattr(gitops_deploy, constant), marker


def test_the_marker_table_covers_every_constant_and_no_more():
    """Non-vacuity for the loop above: it names fifteen markers, and so must the table."""
    assert len(deploy_io.DeployerState.MARKERS) == 15


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
