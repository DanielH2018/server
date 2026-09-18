"""Tests for the shared parked-deployer decision (issue #1429).

Two readers ask it — the SessionStart banner and `deploy.sh` exit 4 — so every rule here is a
must-fire / must-not-fire pair: a decision that answered "parked" for everything and one that
answered it for nothing are indistinguishable from the passing side alone.

Run: uv run pytest scripts/lib/tests/test_deployer_park.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.deployer_park import (
    BEHIND_PARK_SECONDS,
    park_age,
    park_note,
    read_behind_marker,
    read_manual_plane_marker,
)
from lib.gitops_markers import MARKERS

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


# ── the manual_plane marker (issue #1774); its parser is tested with `gitops_markers` ──────
# The shape the deployer writes: origin SHA, the playbook that applies the role (or `none`),
# the role, and when it was first recorded.
_PENDING = (
    "abc1230000000000000000000000000000000000 ansible/k3s-bringup.yml k3s 1000\n"
    "beef1230000000000000000000000000000000000 none common 2000"
)


def test_the_manual_plane_marker_is_read_from_the_state_dir(tmp_path):
    (tmp_path / MARKERS["manual_plane"]).write_text(_PENDING + "\n")
    assert read_manual_plane_marker(str(tmp_path)) == _PENDING


def test_an_absent_manual_plane_marker_reads_as_nothing_pending(tmp_path):
    assert read_manual_plane_marker(str(tmp_path / "nope")) is None
