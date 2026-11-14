"""The restore drill's SQLite integrity check, in two halves.

The drill half runs the rendered script through the stubbed `kubectl`, with the probe pod's log
set by `K3S_STUB_PROBE`, and asserts what the run stamps. The probe half lifts the Python program
the probe pod runs out of the rendered script and runs it against real SQLite files in a tmp
directory, because the stub never runs the pod and so cannot show the program can go red.

Run: uv run pytest tests/longhorn/test_longhorn_restore_drill_sqlite.py
"""

import json
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


def test_the_probe_pod_runs_the_program_over_a_read_only_mount(tmp_path: Path) -> None:
    """The pod spec delivers the lifted program, its scratch emptyDir and a read-only restore."""
    run = harness(tmp_path, [PVC])
    proc = run(restore="pass")
    assert proc.returncode == 0, proc.stderr
    created = [c for c in run.calls if " run restore-drill-" in c]
    assert len(created) == 1, run.calls
    spec = json.loads(created[0].split("--overrides=", 1)[1])["spec"]
    (container,) = spec["containers"]
    assert container["command"][:2] == ["sh", "-c"]
    assert 'python3 -c "$SQLITE_CHECK" /drill /scratch' in container["command"][2]
    assert {"name": "SQLITE_CHECK", "value": _probe_program()} in container["env"]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert mounts["/drill"]["readOnly"] is True
    assert volumes[mounts["/drill"]["name"]]["persistentVolumeClaim"]["claimName"] == (
        f"restore-drill-{PVC}"
    )
    assert "emptyDir" in volumes[mounts["/scratch"]["name"]]


def test_the_probe_skips_a_fifo_instead_of_blocking_on_it(tmp_path: Path) -> None:
    """Opening a named pipe waits for a writer; the scan reads regular files only."""
    root = tmp_path / "drill"
    root.mkdir()
    _make_db(root / "good.db")
    os.mkfifo(root / "pipe")
    proc = launch(
        [sys.executable, "-I", "-c", _probe_program(), str(root), str(tmp_path)],
        env={"PATH": os.environ["PATH"]},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "sqlite ok good.db",
        "sqlite_checked=1 sqlite_bad=0",
    ]


def _wal_only_corruption(conn: sqlite3.Connection) -> None:
    """Point the index at the table's root page, a schema change that lives only in the WAL."""
    root = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = 't'"
    ).fetchone()
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET rootpage = ? WHERE name = 't_v'", root)
    conn.commit()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the read-only mode bits this test relies on"
)
@pytest.mark.parametrize(
    ("corrupt_wal", "verdict"),
    [(False, "sqlite ok app.db"), (True, "sqlite bad app.db: ")],
)
def test_a_database_a_read_only_open_refuses_is_checked_from_a_copy(
    tmp_path: Path, corrupt_wal: bool, verdict: str
) -> None:
    """A `-wal` without its `-shm` cannot open read-only, so the copy with its WAL is checked.

    The main file holds only the clean state, and the corruption exists only in the WAL. So the
    corrupt case reads `bad` only if the copy took the WAL with it and replayed it.
    """
    live = tmp_path / "live"
    live.mkdir()
    writer = sqlite3.connect(live / "app.db")
    writer.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    writer.execute("CREATE INDEX t_v ON t(v)")
    writer.executemany("INSERT INTO t(v) VALUES (?)", [(f"v{i}",) for i in range(2000)])
    writer.commit()
    writer.execute("PRAGMA journal_mode=wal")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executemany("INSERT INTO t(v) VALUES (?)", [(f"w{i}",) for i in range(500)])
    writer.commit()
    if corrupt_wal:
        _wal_only_corruption(writer)
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
    lines = proc.stdout.splitlines()
    assert lines[0].startswith(verdict), lines
    assert lines[1:] == [f"sqlite_checked=1 sqlite_bad={int(corrupt_wal)}"]
    assert not any((tmp_path / "scratch").iterdir()), "the copy must be removed"
