"""`DeployerState`'s `manual_plane` ledger class: recording, narrowing and clearing a role.

The class lives in the `owed` ledger (#3392); it replaced the `manual_plane` line marker and
its `manual_plane_tags` sidecar, whose readers went in #3487. Split from `test_deployer_state.py`, which pins the on-disk layout.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deployer_state_manual_plane.py
"""

import json
import os
import pathlib

import pytest

import deploy_io

SHA = "c0ffee12" * 5


@pytest.fixture
def state(tmp_path: pathlib.Path) -> deploy_io.DeployerState:
    return deploy_io.DeployerState(tmp_path)


# ── the manual_plane marker ───────────────────────────────────────────────────────────────
# The two setup roles `initial_setup.yml` cannot apply, as the marker records them: k3s is
# applied by k3s-bringup.yml, common by no playbook at all.
K3S_LINE = ("ansible/k3s-bringup.yml", "k3s")
COMMON_LINE = ("none", "common")


def test_a_pending_role_is_recorded_as_one_ledger_line(state):
    """The writer records one ledger line and no other file (#3392)."""
    assert state.record_manual_plane(SHA, *K3S_LINE, 1000.0) is True
    (line,) = pathlib.Path(state.path("owed")).read_text().splitlines()
    assert json.loads(line) == {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": SHA,
        "at": 1000,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": [],
    }
    files = {
        f.name for f in pathlib.Path(state.path("owed")).parent.iterdir() if f.is_file()
    }
    assert files == {"owed.jsonl"}
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


def test_clearing_the_last_role_removes_the_ledger_entirely(state):
    """An empty file reads as None everywhere else, so leave none behind."""
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.clear_manual_plane("k3s")
    assert state.manual_plane_pending() == []
    assert not os.path.exists(state.path("owed"))


# ── the narrow tags of a pending role ─────────────────────────────────────────────


def test_the_narrow_tags_of_a_pending_role_round_trip(state):
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    assert state.manual_plane_tags_pending() == {"k3s": frozenset({"kubeconfig"})}


def test_a_second_range_on_the_same_role_unions_its_tags(state):
    """Both changes are merged and unapplied, so both tags have to run."""
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
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
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    state.record_manual_plane_tags(
        "k3s", frozenset({"kubeconfig"}), line_predates=False
    )
    state.record_manual_plane_tags("k3s", None, line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}
    state.record_manual_plane_tags("k3s", frozenset({"coredns"}), line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}, (
        "a refusal already recorded is not narrowed away by a later range"
    )


def test_a_line_whose_range_was_never_narrowed_is_flagged_as_the_whole_role(state):
    """Unknown joined with anything is the whole role.

    A line recorded with no tags says nothing about what its range needed. Narrowing it to the
    NEXT range's answer would leave the first change unapplied behind a clear command.
    """
    state.record_manual_plane(SHA, *K3S_LINE, 1000.0)
    assert state.record_manual_plane("beef" * 10, *K3S_LINE, 2000.0) is False
    state.record_manual_plane_tags("k3s", frozenset({"kubeconfig"}), line_predates=True)
    assert state.manual_plane_tags_pending() == {"k3s": frozenset()}


def test_a_key_the_writer_does_not_know_survives_a_tags_rewrite(state):
    """The ledger's whole point: a newer writer's key is carried, not erased (#3392)."""
    line = {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": SHA,
        "at": 1000,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": ["kubeconfig"],
        "reason": "from a newer deployer",
    }
    pathlib.Path(state.path("owed")).write_text(json.dumps(line) + "\n")
    state.record_manual_plane_tags("k3s", frozenset({"coredns"}), line_predates=True)
    (written,) = pathlib.Path(state.path("owed")).read_text().splitlines()
    assert json.loads(written) == {**line, "tags": ["coredns", "kubeconfig"]}


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
    assert not os.path.exists(state.path("owed"))


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
    torn = json.dumps({"class": "manual_plane", "subject": "k3s", "at": "not-a-number"})
    pathlib.Path(state.path("owed")).write_text(f"{torn}\ngarbage\n")
    assert state.manual_plane_pending() == []
    state.record_manual_plane(SHA, *COMMON_LINE, 2000.0)
    assert "garbage" in pathlib.Path(state.path("owed")).read_text()
    assert [e.role for e in state.manual_plane_pending()] == ["common"]


def test_the_marker_key_is_the_role_name_for_every_pending_role():
    """An operator clears by the role name the alert prints, so the two must be one word.

    The marker's third field is `setup_role_tag(role)`, and only a role
    `initial_setup.yml` does not apply can ever be written there. Both of those roles are
    tagged by their own name today. A future one that is not (the `chezmoi_setup` /
    `chezmoi` shape) would make `clear-owed manual_plane <role>` miss its line, so it fails here
    rather than on a host.
    """
    import deploy_changes

    roles = set(deploy_changes._SETUP_ROLES_OUTSIDE_INITIAL_SETUP)
    assert roles >= {"k3s", "common"}, roles
    for role in roles:
        assert deploy_changes.setup_role_tag(role) == role, role
