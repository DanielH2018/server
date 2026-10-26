"""Tests for `probe_lib/host_reader.py`, the one-process read the host probes share.

The READER tests run the real script with the local `python3`, the path `read_host` takes on
the host it reads, against files in `tmp_path`.
"""

import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import core, host_reader
from lib.proc_testing import path_with, write_exec

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
    doc = _local(
        {"files": [str(present), str(tmp_path / "gone")], "stat": [str(present)]}
    )
    assert doc["files"][str(present)] == {"status": "ok", "text": "x 1\n"}
    assert doc["files"][str(tmp_path / "gone")] == {"status": "absent"}
    row = doc["stat"][str(present)]
    assert row["mode"] == "0640"
    assert row["sha256"] == hashlib.sha256(b"x 1\n").hexdigest()


def test_an_unreadable_file_keeps_its_stat_and_drops_only_its_hash(tmp_path):
    secret = tmp_path / "registry-gc.sh"
    secret.write_text("token\n")
    secret.chmod(0o000)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("running as root, which reads a 0000 file")
        row = _local({"stat": [str(secret)]})["stat"][str(secret)]
    finally:
        secret.chmod(0o600)
    assert row["status"] == "ok" and row["mode"] == "0000"
    assert row["sha256"] is None and "Permission denied" in row["content"]


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


def test_a_file_this_user_stats_but_cannot_open_is_hashed_by_rootq(
    tmp_path, monkeypatch
):
    """Only `matches` is asked: the reader's own lstat already has the metadata (#6357)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "rootq.argv"
    bin_dir.mkdir()
    write_exec(bin_dir / "rootq", f'#!/bin/sh\necho "$*" >> {log}\necho match\n')
    monkeypatch.setenv("PATH", path_with(bin_dir))
    script = tmp_path / "x.sh"
    script.write_text("exec x\n")
    script.chmod(0o000)
    digest = hashlib.sha256(b"exec x\n").hexdigest()
    try:
        if os.access(script, os.R_OK):
            pytest.skip("running as root, which reads a 0000 file")
        row = _local({"stat": [str(script)], "rootq": {str(script): digest}})
    finally:
        script.chmod(0o600)
    row = row["stat"][str(script)]
    assert (row["mode"], row["match"], "via" in row) == ("0000", True, False)
    assert log.read_text().splitlines() == [f"matches {script} {digest}"]


def test_a_path_rootq_finds_missing_is_absent_not_unreadable(tmp_path, monkeypatch):
    """An absent arm's file under a directory this user cannot traverse (#6357)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    refusal = 'echo "refused: $2 does not exist" >&2\nexit 2\n'
    write_exec(bin_dir / "rootq", "#!/bin/sh\n" + refusal)
    monkeypatch.setenv("PATH", path_with(bin_dir))
    locked = tmp_path / "rules.d"
    locked.mkdir()
    locked.chmod(0)
    gone = str(locked / "50-claude-land.rules")
    try:
        if os.access(locked, os.X_OK):
            pytest.skip("running as root, which traverses a 0000 directory")
        row = _local({"stat": [gone], "rootq": {gone: None}})["stat"][gone]
    finally:
        locked.chmod(0o700)
    assert row == {"status": "absent", "via": "rootq"}
