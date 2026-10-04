"""Tests for the shared parked-deployer decision.

Two readers ask it — the SessionStart banner and `deploy.sh` exit 4 — so every rule here is a
must-fire / must-not-fire pair: a decision that answered "parked" for everything and one that
answered it for nothing are indistinguishable from the passing side alone.

Run: uv run pytest scripts/lib/tests/test_deployer_park.py
"""

import json

from lib.deployer_park import (
    BEHIND_PARK_SECONDS,
    manual_plane_lines,
    park_age,
    park_note,
    read_behind_marker,
    read_k8s_unapplied_marker,
    read_manual_plane_marker,
    read_manual_plane_tags_marker,
)
from gitops_ledger import OWED_K8S_UNAPPLIED, owed_line, parse_owed
from gitops_markers import MARKERS, parse_manual_plane

# The shape the deployer writes: the origin SHA it is behind, then when it first saw it.
_MARKER = "abc1230000000000000000000000000000000000 1000"


def test_a_stamp_older_than_the_threshold_is_a_park():
    assert park_age(_MARKER, 1000 + BEHIND_PARK_SECONDS + 60) is not None


def test_a_stamp_within_the_threshold_is_a_queue():
    assert park_age(_MARKER, 1000 + BEHIND_PARK_SECONDS - 60) is None


def test_the_age_is_measured_from_the_stamp_not_from_now():
    """The banner prints minutes, so the number has to be the age of the marker."""
    assert park_age(_MARKER, 1000 + 3600) == 3600


def test_an_absent_marker_is_not_a_park():
    assert park_age(None, 1e9) is None
    assert park_age("", 1e9) is None


def test_a_torn_marker_is_not_a_park():
    """Written atomically, so a value that will not parse is damage, never an old park."""
    assert park_age("not-a-timestamp", 1e9) is None


def test_the_note_names_the_primary_checkout_when_parked():
    note = park_note(_MARKER, 1000 + 3600)
    assert "primary checkout" in note
    assert "60 min" in note
    assert "journalctl -t gitops-deploy" in note


def test_the_note_is_empty_when_the_deployer_is_merely_behind():
    assert park_note(_MARKER, 1000 + BEHIND_PARK_SECONDS - 60) == ""


def test_the_marker_is_read_from_the_state_directory(tmp_path):
    (tmp_path / MARKERS["behind"]).write_text(_MARKER + "\n")
    assert read_behind_marker(str(tmp_path)) == _MARKER


def test_a_missing_state_directory_reads_as_no_marker(tmp_path):
    """Every host but daniel-box has none, and that must not be an error."""
    assert read_behind_marker(str(tmp_path / "nope")) is None


# ── the manual_plane marker; its parser is tested with `gitops_markers` ──────
# The shape the deployer writes: origin SHA, the playbook that applies the role (or `none`),
# the role, and when it was first recorded.
_PENDING = (
    "abc1230000000000000000000000000000000000 ansible/k3s-bringup.yml k3s 1000\n"
    "beef1230000000000000000000000000000000000 none common 2000"
)


def test_the_manual_plane_marker_is_read_from_the_state_dir(tmp_path):
    (tmp_path / MARKERS["manual_plane"]).write_text(_PENDING + "\n")
    marker = read_manual_plane_marker(str(tmp_path))
    assert parse_manual_plane(marker) == parse_manual_plane(_PENDING)


def test_a_manual_plane_ledger_line_with_an_unknown_key_reaches_the_banner(tmp_path):
    """The Verify-by of #3392 for the banner: a key a newer writer added is ignored.

    The role is pending in the line marker too, with no sidecar row, so its needs are
    unknown there and the whole-role clear wins over the ledger's narrower tags.
    """
    (tmp_path / MARKERS["manual_plane"]).write_text(_PENDING + "\n")
    line = {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": "f" * 40,
        "at": 500,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": ["kubeconfig"],
        "added_by_a_newer_writer": 1,
    }
    (tmp_path / MARKERS["owed"]).write_text(json.dumps(line) + "\n")
    state = str(tmp_path)
    lines = manual_plane_lines(
        read_manual_plane_marker(state), 4000, read_manual_plane_tags_marker(state)
    )
    assert len(lines) == 2, lines
    assert "`k3s` setup role 58 min ago" in lines[0], "the older ledger stamp decides"
    assert lines[0].endswith("clear-manual-plane k3s`")
    assert "`common` setup role" in lines[1]


def test_an_absent_manual_plane_marker_reads_as_nothing_pending(tmp_path):
    assert read_manual_plane_marker(str(tmp_path / "nope")) is None


def test_k8s_unapplied_is_read_from_the_owed_ledger(tmp_path):
    sha = "a" * 40
    (tmp_path / MARKERS["owed"]).write_text(
        owed_line(OWED_K8S_UNAPPLIED, "authelia", sha, 1000) + "\n"
    )
    entries = parse_owed(read_k8s_unapplied_marker(str(tmp_path)), OWED_K8S_UNAPPLIED)
    assert [(e.subject, e.at) for e in entries] == [("authelia", 1000.0)]


def test_k8s_unapplied_with_no_ledger_reads_as_nothing_pending(tmp_path):
    assert read_k8s_unapplied_marker(str(tmp_path)) is None
