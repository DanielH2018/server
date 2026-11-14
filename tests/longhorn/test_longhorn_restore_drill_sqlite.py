"""The restore drill's SQLite integrity check, in two halves.

The drill half runs the rendered script through the stubbed `kubectl`, with the probe pod's log
set by `K3S_STUB_PROBE`, and asserts what the run stamps. The probe half lifts the Python program
the probe pod runs out of the rendered script and runs it against real SQLite files in a tmp
directory, because the stub never runs the pod and so cannot show the program can go red.

Run: uv run pytest tests/longhorn/test_longhorn_restore_drill_sqlite.py
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from _restore_drill import DRILL_TEMPLATE, harness
from _shell_render import rendered_shell_text
from lib.proc_testing import run as launch

PVC = "some-config"


def _probe_log(*lines: str, checked: int, bad: int) -> str:
    return "\n".join(
        ["files=3 bytes=4096", *lines, f"sqlite_checked={checked} sqlite_bad={bad}"]
    )


def test_a_corrupt_database_fails_the_drill_and_stamps_no_success(
    tmp_path: Path,
) -> None:
    """FLAGGED half: a database failing integrity_check is a drill failure."""
    # fact: k8s/roles/longhorn-restore-drill/CLAUDE.md#What the drill is and what it writes (the autonomous-role contract)
    run = harness(tmp_path, [PVC])
    log = _probe_log(
        "sqlite ok app.db",
        "sqlite bad home-assistant_v2.db: database disk image is malformed",
        checked=2,
        bad=1,
    )
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 1
    assert "home-assistant_v2.db" in proc.stderr, proc.stderr
    assert "last-success" not in run.cm
    assert f"success.{PVC}" not in run.cm
    assert run.cm[f"sqlite.{PVC}"].endswith(" checked=2 bad=1")


def test_clean_databases_pass_and_record_the_result(tmp_path: Path) -> None:
    """CLEAN half: the result is stamped beside the success it belongs to."""
    run = harness(tmp_path, [PVC])
    log = _probe_log("sqlite ok a.db", "sqlite ok b/c.db", checked=2, bad=0)
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 0, proc.stderr
    assert f"success.{PVC}" in run.cm
    assert run.cm[f"sqlite.{PVC}"].endswith(" checked=2 bad=0")


def test_a_probe_log_without_the_sqlite_summary_fails(tmp_path: Path) -> None:
    """A check that did not finish leaves the databases unknown, not clean."""
    run = harness(tmp_path, [PVC])
    proc = run(
        restore="pass", env={"K3S_STUB_PROBE": "files=3 bytes=4096\nsqlite ok a.db"}
    )
    assert proc.returncode == 1
    assert "reported no result" in proc.stderr, proc.stderr
    assert f"success.{PVC}" not in run.cm


def _probe_program() -> str:
    match = re.search(
        r"<<'PY'\n(.*?)\nPY\n", rendered_shell_text(*DRILL_TEMPLATE), re.DOTALL
    )
    assert match, "the drill's SQLite program moved; update _probe_program"
    return match.group(1)


def _run_probe(root: Path, scratch: Path) -> subprocess.CompletedProcess:
    scratch.mkdir(exist_ok=True)
    return launch(
        [sys.executable, "-I", "-c", _probe_program(), str(root), str(scratch)],
        env={"PATH": os.environ["PATH"]},
    )


def _make_db(path: Path, rows: int = 2000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("CREATE INDEX t_v ON t(v)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"v{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def test_the_probe_reports_a_corrupt_database_as_bad(tmp_path: Path) -> None:
    """FLAGGED half of the program: overwritten pages are reported, never passed."""
    # fact: k8s/roles/longhorn-restore-drill/CLAUDE.md#What the drill is and what it writes (the autonomous-role contract)
    root = tmp_path / "drill"
    root.mkdir()
    _make_db(root / "good.db")
    _make_db(root / "broken.db")
    data = bytearray((root / "broken.db").read_bytes())
    data[8192:12288] = b"\xff" * 4096
    (root / "broken.db").write_bytes(bytes(data))
    (root / "notes.txt").write_text("not a database")

    proc = _run_probe(root, tmp_path / "scratch")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "sqlite ok good.db" in lines
    assert any(line.startswith("sqlite bad broken.db: ") for line in lines), lines
    assert lines[-1] == "sqlite_checked=2 sqlite_bad=1"


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the read-only mode bits this test relies on"
)
def test_a_database_a_read_only_open_refuses_is_checked_from_a_copy(
    tmp_path: Path,
) -> None:
    """A `-wal` without its `-shm` cannot open read-only; the copy replays it and passes."""
    live = tmp_path / "live"
    live.mkdir()
    writer = sqlite3.connect(live / "app.db")
    writer.execute("PRAGMA journal_mode=wal")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(x)")
    writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    writer.commit()
    root = tmp_path / "drill"
    root.mkdir()
    for name in ("app.db", "app.db-wal"):
        (root / name).write_bytes((live / name).read_bytes())
    writer.close()
    (root / "app.db").chmod(0o444)
    (root / "app.db-wal").chmod(0o444)
    root.chmod(0o555)
    try:
        proc = _run_probe(root, tmp_path / "scratch")
    finally:
        root.chmod(0o755)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "sqlite ok app.db",
        "sqlite_checked=1 sqlite_bad=0",
    ]
    assert not any((tmp_path / "scratch").iterdir()), "the copy must be removed"
