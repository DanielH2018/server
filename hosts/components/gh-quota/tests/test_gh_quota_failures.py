"""How the GitHub spend ledger fails: a failed sample, a locked or corrupt file, the
failure log, and two writers at once.

Run: uv run pytest hosts/components/gh-quota/tests/test_gh_quota_failures.py
"""

import dataclasses
import os
import sqlite3
import subprocess
import threading

import pytest
from gh_quota import KEEP_S

from _gh_quota_fakes import NOW, SESSION, FakeGh


@pytest.mark.parametrize(
    "failure",
    [
        OSError("gh is not installed"),
        subprocess.CalledProcessError(1, ["gh", "api", "rate_limit"]),
    ],
    ids=["gh-missing", "gh-exits-nonzero"],
)
def test_a_failed_sample_is_counted_and_the_call_is_still_recorded(recording, failure):
    ledger, clock, gh = recording
    gh.fail = failure

    ledger.record(["pr", "list"])

    assert ledger.failures() == 1
    gh.fail = None
    clock.t += 31
    (window,) = ledger.windows()
    assert window.sessions == {SESSION: {"pr list": 1}}


@pytest.mark.parametrize(
    "reply",
    ["{}", '{"message": "Bad credentials"}', '{"resources": null}', "[]", "not json"],
    ids=["empty", "error-body", "null-resources", "list", "not-json"],
)
def test_a_rate_limit_reply_without_resources_is_counted_and_the_call_is_recorded(
    recording, reply
):
    ledger, clock, gh = recording
    gh.reply = reply

    ledger.record(["pr", "list"])

    assert ledger.failures() == 1
    gh.reply = None
    clock.t += 31
    (window,) = ledger.windows()
    assert window.sessions == {SESSION: {"pr list": 1}}


def test_a_call_waits_out_another_writer_holding_the_ledger_for_seconds(recording):
    ledger, _clock, _gh = recording
    ledger.record(["pr", "list"])
    (path,) = ledger.root.glob("*.sqlite")
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    release = threading.Timer(1.5, other.execute, ["COMMIT"])
    release.start()
    try:
        ledger.record(["pr", "list"])
    finally:
        release.join()
        other.close()

    assert ledger.failures() == 0
    (window,) = ledger.windows()
    assert window.sessions == {SESSION: {"pr list": 2}}


def test_a_call_made_while_another_caller_samples_github_is_recorded(recording):
    ledger, _clock, gh = recording
    other = dataclasses.replace(
        ledger, env={"CLAUDE_CODE_SESSION_ID": "other"}, run=FakeGh()
    )
    gh.during_sample = lambda: other.record(["pr", "view"])

    ledger.record(["pr", "list"])

    assert ledger.failures() == 0
    (window,) = ledger.windows()
    assert window.sessions == {SESSION: {"pr list": 1}, "other": {"pr view": 1}}


def test_record_lets_the_call_through_when_the_ledger_file_is_corrupt(recording):
    ledger, _clock, _gh = recording
    ledger.record(["pr", "list"])
    (path,) = ledger.root.glob("*.sqlite")
    path.write_bytes(
        b"not a database, and longer than a SQLite header needs to be to say so"
    )

    ledger.record(["pr", "list"])

    assert ledger.failures() == 1


def test_a_failure_older_than_the_ledger_keeps_its_rows_is_not_counted(recording):
    ledger, clock, gh = recording
    gh.fail = OSError("gh is not installed")
    ledger.record(["pr", "list"])
    gh.fail = None

    clock.t += KEEP_S - 60
    assert ledger.failures() == 1
    clock.t += 120
    assert ledger.failures() == 0


def test_a_failure_log_at_its_size_limit_drops_its_old_lines_and_takes_the_new_one(
    recording,
):
    ledger, _clock, gh = recording
    log = ledger.root / f"failures.{os.getuid()}"
    recent = f"{int(NOW) - 60} OSError: disk full"
    log.write_text(
        f"{int(NOW) - KEEP_S - 60} OSError: disk full\n" * 4000 + f"{recent}\n"
    )
    gh.fail = OSError("gh is not installed")

    ledger.record(["pr", "list"])

    assert log.read_text().splitlines() == [
        recent,
        f"{int(NOW)} {SESSION} OSError: gh is not installed",
    ]


def test_a_failure_log_full_of_recent_lines_takes_no_new_line(recording):
    ledger, _clock, gh = recording
    log = ledger.root / f"failures.{os.getuid()}"
    full = f"{int(NOW) - 60} OSError: disk full\n" * 4000
    log.write_text(full)
    gh.fail = OSError("gh is not installed")

    ledger.record(["pr", "list"])

    assert log.read_text() == full
