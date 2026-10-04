"""`DeployerState` reads the same files, at the same paths, with the same three outcomes.

`deploy_io.DeployerState` wraps the marker files, and `MARKERS` is the only table of them.
This module pins the on-disk layout:

- the table is exactly the named pairs below, and each resolves under the host directory;
- a MISSING file, an EMPTY file and an UNREADABLE directory are told apart — the first two
  read as None, the third RAISES.

That third case is the one worth writing down. The reader catches `FileNotFoundError` only,
so a state directory with the wrong mode propagates an `OSError` and the tick pages. A bare
`except OSError` would make a HELD host report converged, because "cannot read hold_sha" and
"there is no hold" would produce the same answer.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py
"""

import json
import os
import pathlib

import pytest
import yaml

import deploy_io
from gitops_ledger import (
    OWED_HOLD_PLANE,
    OWED_K8S_DEFERRED,
    RECEIPT_KEEP,
    owed_line,
    parse_owed,
    parse_receipts,
)

SHA = "c0ffee12" * 5
ROLE = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path: pathlib.Path) -> deploy_io.DeployerState:
    return deploy_io.DeployerState(tmp_path)


# ── the paths did not move ────────────────────────────────────────────────────────────────
# Every marker by name, as `MARKERS` key -> basename on disk. A frozenset of pairs rather
# than a count: a renamed basename or a swapped pair fails naming the marker, where a count
# would pass either. Add a pair here when a marker is added, and nowhere else.
EXPECTED_MARKERS = frozenset(
    {
        ("hold", "hold_sha"),
        ("hold_plane", "hold_plane"),
        ("contention", "contention_since"),
        ("owed", "owed.jsonl"),
        ("receipts", "receipts.jsonl"),
        ("last_run", "last_run"),
        ("diverged", "diverged_sha"),
        ("behind", "behind_since"),
        ("alerted", "alerted_shas"),
        ("denylist_rendered", "denylist_rendered_sha"),
        ("dirty_alerted", "dirty_alerted_date"),
        ("pending_alerts", "pending_alerts.json"),
    }
)


def test_the_marker_table_is_exactly_the_named_census():
    assert frozenset(deploy_io.DeployerState.MARKERS.items()) == EXPECTED_MARKERS


def test_every_marker_resolves_under_the_host_state_directory():
    live = deploy_io.DeployerState(deploy_io.STATE_DIR)
    for marker, basename in EXPECTED_MARKERS:
        assert live.path(marker) == f"{deploy_io.STATE_DIR}/{basename}", marker


def _retired_basenames_install_reaps() -> list[str]:
    tasks = yaml.safe_load((ROLE / "tasks" / "install.yml").read_text())
    (reap,) = [
        t
        for t in tasks
        if t["name"] == "Remove the state files of retired deployer markers"
    ]
    assert reap["ansible.builtin.file"]["state"] == "absent"
    return reap["loop"]


def test_the_install_reaps_the_retired_markers():
    # Named rather than counted: a reap list emptied by a refactor must fail here, not pass.
    reaped = set(_retired_basenames_install_reaps())
    assert {"broad_applied", "manual_plane", "manual_plane_tags"} <= reaped


def test_the_install_reaps_no_live_marker():
    # A marker joins the reap list in the PR that retires it. One still in the table here would
    # be deleted on every apply while the deployer still reads and writes it.
    live = {basename for _, basename in EXPECTED_MARKERS}
    assert not live & set(_retired_basenames_install_reaps())


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


# ── the contention_since marker ─────────────────────────────────────────────
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


# ── the k8s_deferred class of the owed ledger ──────────────────────────────────
def test_a_deferred_bump_keeps_its_first_seen_stamp_and_its_origin(state):
    """The age monitor-bridge pages on, so a second deferral must not reset it.

    THE ORIGIN IS KEPT TOO, where `k8s_unapplied` advances it. This marker is cleared
    by a tick deploying the service, never off the origin, and `deploy_defer.unrecord` resets
    the tree — so an advanced SHA here would name a commit the host no longer carries.
    """
    assert state.record_k8s_deferred(SHA, {"sonarr"}, 1000.0) == ["sonarr"]
    assert state.record_k8s_deferred("f" * 40, {"sonarr"}, 9000.0) == []
    assert [(e.origin, e.service, e.at) for e in state.k8s_deferred_pending()] == [
        (SHA, "sonarr", 1000.0)
    ]
    assert [e.subject for e in parse_owed(state.read("owed"), OWED_K8S_DEFERRED)] == [
        "sonarr"
    ], "the bump is a ledger class (#3392)"


def test_a_key_this_writer_does_not_know_survives_a_record(state):
    """A newer writer's key is carried, not dropped: the reason the class left the line format."""
    state.write(
        "owed",
        json.dumps(
            {
                "at": 1000,
                "class": "k8s_deferred",
                "new": 1,
                "origin": SHA,
                "subject": "sonarr",
            }
        ),
    )
    state.record_k8s_deferred("f" * 40, {"sonarr", "radarr"}, 9000.0)
    assert json.loads(state.read("owed").splitlines()[0])["new"] == 1


def test_clearing_one_deferred_bump_leaves_the_others(state):
    state.record_k8s_deferred(SHA, {"sonarr", "radarr"}, 1000.0)
    assert state.clear_k8s_deferred({"sonarr"}) == ["sonarr"]
    assert [e.service for e in state.k8s_deferred_pending()] == ["radarr"]
    assert state.clear_k8s_deferred({"radarr"}) == ["radarr"]
    assert state.read("owed") is None


def test_a_garbled_k8s_deferred_line_is_carried_through_a_clear(state):
    """Skipped by every reader, never dropped: it is the only record of a deferral.

    The REJECTING half of the #2657 repair: this line names nobody, so no clear and no record
    can act on it without guessing.
    """
    state.write("owed", f"garbage\n{owed_line(OWED_K8S_DEFERRED, 'sonarr', SHA, 1000)}")
    assert state.clear_k8s_deferred({"sonarr"}) == ["sonarr"]
    assert state.read("owed") == "garbage"


_TORN_SONARR = json.dumps({"class": "k8s_deferred", "subject": "sonarr", "origin": SHA})


def test_a_torn_k8s_deferred_line_is_repaired_at_its_own_origin(state):
    """This class keeps the recorded origin where `k8s_unapplied` advances it, repair included.

    A tick clears `k8s_deferred` by deploying the service and `deploy_defer.unrecord` can reset
    the tree, so an advanced SHA here would name a commit the host no longer carries.
    """
    state.write("owed", _TORN_SONARR)
    assert state.record_k8s_deferred("f" * 40, {"sonarr"}, 9000.0) == []
    assert [(e.origin, e.service, e.at) for e in state.k8s_deferred_pending()] == [
        (SHA, "sonarr", 9000.0)
    ]


def test_a_torn_k8s_deferred_line_naming_the_service_is_cleared(state):
    """Nothing else ever drops it: the clear is the only reverse this class has."""
    state.write("owed", _TORN_SONARR)
    assert state.clear_k8s_deferred({"sonarr"}) == ["sonarr"]
    assert state.read("owed") is None


# ── the hold_plane class of the owed ledger ────────────────────────────────────
def _held(state) -> list[tuple[str, str]]:
    return [
        (e.subject, e.origin) for e in parse_owed(state.read("owed"), OWED_HOLD_PLANE)
    ]


def test_a_second_failure_is_held_beside_the_first_and_a_repeat_is_not(state):
    """Each failed apply is its own ledger entry, so each clears on its own (#878's class)."""
    state.hold_failed_apply(SHA, "ansible/deploy.yml", ["radarr"])
    state.hold_failed_apply("f" * 40, "ansible/deploy.yml", ["sonarr"])
    state.hold_failed_apply("e" * 40, "ansible/deploy.yml", ["radarr"])
    assert state.hold_sha == "e" * 40
    assert _held(state) == [
        ("ansible/deploy.yml radarr", SHA),
        ("ansible/deploy.yml sonarr", "f" * 40),
    ]
    assert state.read("hold_plane") is None, "the writer records no line marker"


def test_an_apply_drops_only_the_plane_it_covers_and_the_last_one_clears_the_hold(
    state,
):
    state.hold_failed_apply(SHA, "ansible/deploy.yml", ["radarr"])
    state.hold_failed_apply(SHA, "ansible/initial_setup.yml", ["gitops_deploy"])
    state.clear_service_hold({"radarr"})
    assert state.hold_sha == SHA
    assert state.hold_plane == "ansible/initial_setup.yml gitops_deploy"
    state.clear_broad_hold("ansible/initial_setup.yml", [])
    assert (state.hold_sha, state.read("owed")) == (None, None)


def test_a_legacy_hold_plane_marker_is_folded_into_the_ledger_in_order(state):
    """A marker a pre-ledger deployer wrote moves on the first hold, at `hold_sha`."""
    state.write("hold", SHA)
    state.write("hold_plane", "ansible/deploy.yml radarr; ansible/deploy.yml sonarr")
    state.hold_failed_apply("f" * 40, "ansible/deploy.yml", ["sonarr"])
    assert _held(state) == [
        ("ansible/deploy.yml radarr", SHA),
        ("ansible/deploy.yml sonarr", SHA),
    ]
    assert state.read("hold_plane") is None


def test_a_torn_hold_plane_line_keeps_the_hold(state):
    """A plane the readers skip as torn is still unapplied, so `hold_sha` must not clear."""
    torn = json.dumps({"class": "hold_plane", "subject": "ansible/deploy.yml radarr"})
    state.write("hold", SHA)
    state.write("owed", torn)
    state.clear_broad_hold("ansible/initial_setup.yml", [])
    assert (state.hold_sha, state.read("owed")) == (SHA, torn)
    state.clear_service_hold(set())
    assert (state.hold_sha, state.read("owed")) == (SHA, torn)
    state.clear_service_hold({"radarr"})
    assert (state.hold_sha, state.read("owed")) == (None, None)


# ── the alert dedupe slots, and their one file ─────────────────────────
def test_every_alert_slot_lives_in_the_one_keyed_file(state):
    """Each slot round-trips, and writing one leaves the others standing."""
    state.record_alerted("broad", SHA)
    state.record_alerted("ci", "deadbeef" * 5)
    assert state.alerted_sha("broad") == SHA
    assert state.alerted_sha("ci") == "deadbeef" * 5
    assert state.alerted_sha("tasks") is None
    assert pathlib.Path(state.path("alerted")).read_text() == (
        f"broad {SHA}\nci {'deadbeef' * 5}"
    )


def test_clearing_a_slot_leaves_the_others_and_removes_an_emptied_file(state):
    state.record_alerted("broad", SHA)
    state.record_alerted("ci", SHA)
    state.clear_alerted("broad")
    assert state.alerted_sha("broad") is None
    assert state.alerted_sha("ci") == SHA
    state.clear_alerted("ci")
    assert not pathlib.Path(state.path("alerted")).exists()


def test_an_unknown_alert_slot_is_a_typo_not_a_new_channel(state):
    """Each slot name is validated, so a misspelling raises instead of opening a channel."""
    with pytest.raises(KeyError):
        state.record_alerted("k9s", SHA)
    with pytest.raises(KeyError):
        state.alerted_sha("k9s")


# ── the per-SHA tick receipt (#3391) ─────────────────────────────────────────────────────


def test_a_receipt_merges_every_plan_of_one_origin_into_one_line(state):
    """A setup-then-deploy range applies twice and records a pending role once: one line."""
    state.record_receipt(
        "o" * 40, "b" * 40, applied={"ansible/initial_setup.yml": ["x"]}
    )
    state.record_receipt("o" * 40, "b" * 40, manual={"k3s": frozenset({"kubeconfig"})})
    state.record_receipt("o" * 40, "b" * 40, applied={"ansible/deploy.yml": []})
    (receipt,) = parse_receipts(state.read("receipts"))
    assert receipt.applied == {
        "ansible/initial_setup.yml": ("x",),
        "ansible/deploy.yml": (),
    }
    assert receipt.manual == {"k3s": frozenset({"kubeconfig"})}


def test_a_receipt_keeps_a_key_this_writer_does_not_know(state):
    state.write("receipts", json.dumps({"origin": "o" * 40, "base": "", "hold": "yes"}))
    state.record_receipt("o" * 40, "b" * 40, manual={"k3s": None})
    assert json.loads(state.read("receipts"))["hold"] == "yes"
    assert parse_receipts(state.read("receipts"))[0].manual == {"k3s": frozenset()}


def test_the_receipts_are_bounded_and_drop_takes_out_one_origin(state):
    for n in range(RECEIPT_KEEP + 5):
        state.record_receipt(f"{n:040d}", "", applied={"ansible/deploy.yml": []})
    origins = [r.origin for r in parse_receipts(state.read("receipts"))]
    assert origins == [f"{n:040d}" for n in range(5, RECEIPT_KEEP + 5)]
    state.drop_receipt(f"{7:040d}")
    assert f"{7:040d}" not in [r.origin for r in parse_receipts(state.read("receipts"))]
    assert len(parse_receipts(state.read("receipts"))) == RECEIPT_KEEP - 1
