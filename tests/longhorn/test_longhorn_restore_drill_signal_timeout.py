"""A timeout in the restore drill's signal test stays inside that test.

These tests call the signal test from `test_longhorn_restore_drill_state.py` with a drill that
outlasts its wait, then check what the test leaves behind. They live in their own module because
the code they judge is that test's body.

Run: uv run pytest tests/longhorn/test_longhorn_restore_drill_signal_timeout.py
"""

import gc
import importlib
import os
import re
import signal
import subprocess
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from _restore_drill import render

_REAL_POPEN = subprocess.Popen


@dataclass
class _Drill:
    launched: list[subprocess.Popen]
    timed_out: list[float]


def _time_out_the_signal_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Drill:
    """Run the signal test against a drill that ignores the signal, and return its launch."""
    drill = _Drill([], [])

    class RecordingPopen(_REAL_POPEN):
        def __init__(self, *args, **kwargs) -> None:
            self.is_drill = "BASH_FUNC_kubectl%%" in (kwargs.get("env") or {})
            if self.is_drill:
                kwargs["preexec_fn"] = _ignoring_the_signal(kwargs.get("preexec_fn"))
            super().__init__(*args, **kwargs)
            if self.is_drill:
                drill.launched.append(self)

        def wait(self, timeout: float | None = None) -> int:
            if not self.is_drill or timeout is None:
                return super().wait(timeout)
            try:
                return super().wait(min(timeout, 1))
            except subprocess.TimeoutExpired:
                drill.timed_out.append(timeout)
                raise

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    signal_test = importlib.import_module(
        "test_longhorn_restore_drill_state"
    ).test_a_signal_during_the_restore_wait_runs_cleanup_through_the_exit
    workdir = tmp_path / "signal-test"
    workdir.mkdir()
    with pytest.raises((Exception, pytest.fail.Exception)):
        signal_test(workdir, signal.SIGINT, 130)
    monkeypatch.undo()
    assert len(drill.launched) == 1, "the signal test did not launch the drill once"
    assert drill.timed_out, "the signal test failed before its wait timed out"
    return drill


def _ignoring_the_signal(preexec_fn: Callable[[], object] | None) -> Callable[[], None]:
    """Extend `preexec_fn` so bash starts with the signals ignored, which its traps cannot undo."""

    def run() -> None:
        if preexec_fn is not None:
            preexec_fn()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    return run


def test_a_timeout_in_the_signal_test_reaps_its_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _time_out_the_signal_test(tmp_path, monkeypatch).launched[0]
    try:
        assert proc.returncode is not None, f"pid {proc.pid} was never waited for"
    finally:
        proc.kill()
        proc.wait()


def test_a_timeout_in_the_signal_test_leaves_no_warning_for_a_neighbour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `filterwarnings = error`, this warning fails whichever test the collector interrupts."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        drill = _time_out_the_signal_test(tmp_path, monkeypatch)
        pid = drill.launched.pop().pid
        gc.collect()
    leaked = [
        str(w.message)
        for w in caught
        if issubclass(w.category, ResourceWarning) and str(pid) in str(w.message)
    ]
    assert not leaked, leaked


def test_a_signal_the_drill_loses_is_sent_again_and_still_runs_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal test itself asserts the exit status and the `delete volume` call."""
    real_killpg = os.killpg
    sent: list[int] = []
    waits: list[float] = []

    def lose_the_first_signal(pgid: int, sig: int) -> None:
        sent.append(sig)
        if len(sent) > 1:
            real_killpg(pgid, sig)

    class ShortFirstWaitPopen(_REAL_POPEN):
        def wait(self, timeout: float | None = None) -> int:
            if timeout is None:
                return super().wait()
            waits.append(timeout)
            return super().wait(1 if len(waits) == 1 else timeout)

    monkeypatch.setattr(os, "killpg", lose_the_first_signal)
    monkeypatch.setattr(subprocess, "Popen", ShortFirstWaitPopen)
    signal_test = importlib.import_module(
        "test_longhorn_restore_drill_state"
    ).test_a_signal_during_the_restore_wait_runs_cleanup_through_the_exit

    signal_test(tmp_path, signal.SIGINT, 130)

    assert sent == [signal.SIGINT, signal.SIGINT]
    poll_seconds = int(re.findall(r"^ *sleep (\d+)$", render(), re.MULTILINE)[0])
    assert waits[0] > poll_seconds, "a resend this early could land inside cleanup"
