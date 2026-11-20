"""Tests for `probe_lib/host_reader.py`, the one-process read the host probes share.

The READER tests run the real script with the local `python3`, the path `read_host` takes on
the host it reads, against files in `tmp_path`.
"""

import os
import subprocess


import pytest
from diagnostics.probe_lib import core, host_reader

HERE = "daniel-box"


def _local(plan):
    return host_reader.read_host(plan, HERE, hostname=lambda: HERE)


def test_the_host_itself_runs_the_reader_locally_and_any_other_over_one_ssh():
    assert host_reader.reader_argv("daniel-box", "daniel-box.lan") == [
        "python3",
        "-I",
        "-",
    ]
    argv = host_reader.reader_argv("daniel-box", "daniel-server")
    assert argv[0] == "ssh" and argv[-2:] == ["daniel-box", "python3 -I -"]


def test_the_reader_tells_a_present_file_from_an_absent_one(tmp_path):
    present = tmp_path / "builder.prom"
    present.write_text("x 1\n")
    present.chmod(0o640)
    doc = _local({"files": [str(present), str(tmp_path / "gone")]})
    assert doc["files"][str(present)] == {"status": "ok", "text": "x 1\n"}
    assert doc["files"][str(tmp_path / "gone")] == {"status": "absent"}


def test_the_reader_resolves_a_leading_tilde_to_the_reading_users_home(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "record").write_text("x\n")
    doc = _local({"files": ["~/record", "~/gone"]})
    assert doc["files"] == {
        "~/record": {"status": "ok", "text": "x\n"},
        "~/gone": {"status": "absent"},
    }


def test_landing_logs_report_their_verdict_rc_and_liveness(tmp_path):
    (tmp_path / "land6319-1.log").write_text("x\nVERDICT: settled (PR #6319)\n")
    (tmp_path / "land6319-1.rc").write_text("0\n")
    (tmp_path / "land6320-1.log").write_text("running\n")
    (tmp_path / "land6320-1.pid").write_text(f"{os.getpid()}\n")
    rows = _local({"logs": [str(tmp_path), "land*-*.log", 0]})["logs"]["rows"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["land6319-1.log"]["verdict"] == "VERDICT: settled (PR #6319)"
    assert by_name["land6319-1.log"]["rc"] == "0\n"
    assert by_name["land6320-1.log"]["alive"] is True
    assert by_name["land6320-1.log"]["verdict"] is None


def test_a_failed_transport_is_unreadable_not_empty():
    def refused(argv, **_):
        return subprocess.CompletedProcess(argv, 255, "", "ssh: connect refused")

    with pytest.raises(core.Unreadable, match="exit 255"):
        host_reader.read_host({}, "daniel-box", run=refused, hostname=lambda: "x")


def _doc(rc=0, stderr="", entries=()):
    return {
        "host": HERE,
        "user": "claude",
        "journal": {"j": {"rc": rc, "stderr": stderr, "entries": list(entries)}},
    }


def test_a_journal_hidden_from_this_user_is_unreadable_not_no_runs():
    """journalctl exits 0 with no entries for a user outside adm and systemd-journal."""
    hint = (
        "Hint: You are currently not seeing messages from other users and the system."
    )
    with pytest.raises(core.Unreadable, match="read nothing"):
        host_reader.journal_entries(_doc(stderr=hint), "j")


def test_journal_entries_come_back_oldest_first():
    rows = [[20.0, 5, "b", "uv", "second"], [10.0, 3, "a", "uv", "first"]]
    entries = host_reader.journal_entries(_doc(entries=rows), "j")
    assert [e.message for e in entries] == ["first", "second"]
    assert entries[0].priority == 3 and entries[0].invocation == "a"


def test_a_unit_duration_reads_in_seconds_and_a_missing_one_raises():
    assert (
        host_reader.unit_span_s("[Service]\nTimeoutStartSec=20min\n", "TimeoutStartSec")
        == 1200
    )
    with pytest.raises(ValueError, match="no TimeoutStartSec= line"):
        host_reader.unit_span_s("[Service]\n", "TimeoutStartSec")


def test_a_log_directory_behind_an_untraversable_parent_is_unreadable_not_absent(
    tmp_path,
):
    """The agent user on daniel-box cannot traverse the operator's 0750 home."""
    logs = tmp_path / "home" / "logs"
    logs.mkdir(parents=True)
    home = tmp_path / "home"
    mode = home.stat().st_mode
    home.chmod(0o000)
    try:
        if os.access(home, os.X_OK):
            pytest.skip("running as root, which traverses a 0000 directory")
        read = _local({"logs": [str(logs), "land*-*.log", 0]})["logs"]
    finally:
        home.chmod(mode)
    assert read["status"] == "unreadable"
    assert _local({"logs": [str(tmp_path / "gone"), "*", 0]})["logs"] == {
        "status": "absent"
    }


def test_a_pi_read_from_off_the_landing_host_hops_through_it():
    """Only daniel-box holds a key daniel-pi accepts (#8773)."""
    hop = host_reader.reader_argv("daniel-pi", "daniel-server")
    assert hop[-2] == "daniel-box"
    assert hop[-1].endswith("daniel-pi 'python3 -I -'")
    direct = host_reader.reader_argv("daniel-pi", "daniel-box")
    assert direct[-2:] == ["daniel-pi", "python3 -I -"]
