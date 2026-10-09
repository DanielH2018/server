"""Tests for `probe.py gitops-state`, the read-only view of the deployer's markers (#3931).

Each test builds a state directory in `tmp_path` and hands it to `collect`, so none reads the
host's `/var/lib/gitops-deploy`.
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import gitops_view as view

SHA = "2d25ced3" + "0" * 32
NOW = 1_800_000_000.0


def _owed(cls: str, subject: str, at: float = NOW - 3600, **extra) -> str:
    return json.dumps(
        {**extra, "class": cls, "subject": subject, "origin": SHA, "at": int(at)}
    )


def _state(tmp_path, **markers: str):
    for name, text in markers.items():
        (tmp_path / view.MARKERS[name]).write_text(text)
    return tmp_path


def test_every_set_marker_is_printed_with_its_way_out(tmp_path):
    """CLEAN half: the two #3931 names, contention and k8s_deferred, plus the hold's clear."""
    _state(
        tmp_path,
        last_run=f"{NOW - 30}\n",
        hold=SHA,
        contention=f"{SHA} sonarr {NOW - 7200} {NOW - 60} 12\n",
        owed="\n".join(
            [
                _owed("hold_plane", "ansible/deploy.yml sonarr"),
                _owed("k8s_deferred", "radarr"),
                _owed("k8s_unapplied", "homepage"),
            ]
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    assert "last_run:     2027-01-15 07:59:30 UTC (30s ago)" in out
    assert "plane: ansible/deploy.yml sonarr" in out
    assert f"{view.HOLD_CLEAR_CMD} {SHA}" in out
    assert "12 tick(s) deferred on service lock `sonarr`" in out
    assert view.CONTENTION_CLEAR_CMD in out
    assert "radarr  (origin 2d25ced3, 60 min ago)" in out
    assert "gitops_state.py clear-owed k8s_deferred radarr" in out
    assert "gitops_state.py clear-owed k8s_unapplied homepage" in out


def test_hold_plane_lines_with_no_hold_sha_are_shown_as_orphaned(tmp_path):
    """A hand `rm hold_sha` leaves planes the next hold waits on; `none` alone hid them."""
    _state(tmp_path, owed=_owed("hold_plane", "ansible/deploy.yml sonarr"))
    snap = view.collect(tmp_path, NOW)
    assert snap["hold"]["clear"] == f"{view.HOLD_CLEAR_CMD} --orphaned"
    out = view.format_text(snap)
    assert "hold_sha:     none, but 1 orphaned hold_plane line(s)" in out
    assert "plane: ansible/deploy.yml sonarr" in out


def test_absent_markers_read_as_none_and_exit_zero(tmp_path, capsys):
    """An empty state directory is a deployer with nothing to say, which reads cleanly."""
    assert view.run_gitops_state(SimpleNamespace(json=False), tmp_path, NOW) == 0
    out = capsys.readouterr().out
    for line in ("hold_sha:     none", "contention:   none", "k8s_deferred:  none"):
        assert line in out


def test_an_unreadable_marker_is_named_unreadable_not_none(tmp_path, capsys):
    """FLAGGED half: the `claude` user on daniel-box sees `hold_sha` exist and cannot open it.

    `deployer_park`'s readers fold that into absent, which here would print `none` over a
    live hold.
    """
    _state(tmp_path, hold=SHA)
    (tmp_path / "hold_sha").chmod(0o000)
    try:
        assert view.run_gitops_state(SimpleNamespace(json=True), tmp_path, NOW) == 1
    finally:
        (tmp_path / "hold_sha").chmod(0o600)
    snap = json.loads(capsys.readouterr().out)
    assert snap["unreadable"] == ["hold_sha"]
    assert snap["hold"] == {
        "status": "unreadable",
        "sha": None,
        "planes": [],
        "clear": None,
    }


def test_no_state_directory_exits_one_and_says_where_it_lives(tmp_path, capsys):
    """Off daniel-box there is no directory, which must not read as a clean deployer."""
    ns = SimpleNamespace(json=False)
    assert view.run_gitops_state(ns, tmp_path / "missing", NOW) == 1
    out = capsys.readouterr().out
    assert "no state directory here" in out
    assert "hold_sha" not in out


def test_reading_writes_nothing(tmp_path):
    """The view must not tick: the directory is byte-identical after a read."""
    _state(tmp_path, hold=SHA, owed=_owed("k8s_deferred", "radarr"))
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()
    }
    view.collect(tmp_path, NOW)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert after == before
