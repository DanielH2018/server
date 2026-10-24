#!/usr/bin/env python3
"""The restore drill keeps its state in a ConfigMap (H2.3c), and survives a signal and BusyBox.

These tests RUN the rendered script through the shared harness in `_restore_drill.py`, whose fake
API keeps the ConfigMap across runs. They cover the one-time import of the old stamp directory,
the NotFound arm, the stamp-before-restore ordering, the TERM and INT traps, and a `mktemp`
template that BusyBox accepts.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_state.py
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from _restore_drill import harness
from _restore_drill import selected

A = "a-config"
B = "b-config"

_OLD = 1_700_000_000


def _file(path: Path, content: str = "", mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_the_first_run_imports_the_old_stamp_directory_and_a_later_run_does_not(
    tmp_path: Path,
) -> None:
    """The import converts mtimes and contents to keys, and `imported-at` stops a second one."""
    run = harness(tmp_path, [A, B], imported=False)
    stamps = run.stamp_dir
    _file(stamps / "attempts" / A, mtime=_OLD)
    _file(stamps / "retries" / A, mtime=_OLD + 1)
    _file(stamps / "seen" / A, mtime=_OLD - 1)
    _file(stamps / "success" / A, f"{_OLD + 2}\n")
    _file(stamps / "last-success", f"{_OLD + 3}\n")
    _file(stamps / "excluded_nopvc", "pvc-x\n")

    first = run()
    # The imported attempt stamp for A is what makes B, which has none, the rotation's pick.
    assert selected(run, first) == B
    assert {k: run.cm[k] for k in run.cm if k.endswith(f".{A}")} == {
        f"attempts.{A}": str(_OLD),
        f"retries.{A}": str(_OLD + 1),
        f"seen.{A}": str(_OLD - 1),
        f"success.{A}": str(_OLD + 2),
    }
    assert run.cm["last-success"] == str(_OLD + 3)
    imported_at = run.cm["imported-at"]
    assert imported_at.isdigit() and int(imported_at) > _OLD

    _file(stamps / "success" / "late", "5\n")
    run()
    assert run.cm["imported-at"] == imported_at
    assert "success.late" not in run.cm


@pytest.mark.parametrize(
    ("cm", "env", "message"),
    [
        (False, {}, "stamp ConfigMap absent"),
        (True, {"K3S_STUB_GET_CM": "fail"}, "cannot read the stamp ConfigMap"),
    ],
    ids=["not-found", "unreadable"],
)
def test_a_run_that_cannot_read_the_stamps_fails_with_its_own_message_and_restores_nothing(
    tmp_path: Path, cm: bool, env: dict, message: str
) -> None:
    run = harness(tmp_path, [A], cm=cm)
    proc = run(restore="pass", env=env)
    assert proc.returncode == 1
    assert message in proc.stderr, proc.stderr
    assert not [c for c in run.calls if "apply -f" in c or "patch" in c]


def test_a_failed_attempt_stamp_restores_nothing(tmp_path: Path) -> None:
    """The attempt is stamped before the restore, and a stamp that fails stops the run."""
    run = harness(tmp_path, [A])
    proc = run(restore="pass", env={"K3S_STUB_PATCH_FAILS": '"attempts.'})
    assert proc.returncode == 1
    assert "cannot stamp the attempt" in proc.stderr, proc.stderr
    assert not [c for c in run.calls if "apply -f" in c]


def test_the_seen_key_is_written_once_and_never_refreshed(tmp_path: Path) -> None:
    """Only a write-once join marker answers "has this volume had a fair chance"."""
    run = harness(tmp_path, [A, B])
    run.seed({f"seen.{A}": _OLD})
    run()
    assert run.cm[f"seen.{A}"] == str(_OLD)
    assert int(run.cm[f"seen.{B}"]) > _OLD


@pytest.mark.parametrize(
    ("sig", "status"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)]
)
def test_a_signal_during_the_restore_wait_runs_cleanup_through_the_exit(
    tmp_path: Path, sig: signal.Signals, status: int
) -> None:
    """In a pod bash is PID 1 and ignores a signal it has no handler for, so the traps matter.

    The group is signalled, so the script's `sleep 10` poll ends and bash reaches its trap at
    once. Signalling the script alone would also pass, ten seconds later.
    """
    run = harness(tmp_path, [A])
    proc = subprocess.Popen(
        ["bash", str(run.script)],
        env=run.child_env("pass", {"K3S_STUB_VOLSTATE": "creating"}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    try:
        deadline = time.monotonic() + 20
        while not any("get volume restore-drill-" in c for c in run.calls):
            assert time.monotonic() < deadline, "the drill never reached its wait loop"
            time.sleep(0.05)
        os.killpg(proc.pid, sig)
        assert proc.wait(timeout=20) == status
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert any(f"delete volume restore-drill-{A}" in c for c in run.calls), run.calls


# BusyBox hands the template to mkstemp, which requires it to end in XXXXXX; GNU mktemp does not.
_BUSYBOX_MKTEMP = (
    '() { case "$1" in *XXXXXX) command mktemp "$@" ;; '
    '*) echo "mktemp: Invalid argument" >&2; return 1 ;; esac; }'
)


def test_the_manifest_template_ends_in_xxxxxx_for_busybox_mktemp(
    tmp_path: Path,
) -> None:
    """With a suffix, MANIFEST is empty and every run ends in `could not create the drill volume`."""
    run = harness(tmp_path, [A])
    proc = run(restore="pass", env={"BASH_FUNC_mktemp%%": _BUSYBOX_MKTEMP})
    assert "Invalid argument" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr
