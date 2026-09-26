#!/usr/bin/env python3
"""Executing tests for checks 9 and 10 — the trim and B2-accounting crons' evidence plane.

Check 9 reads what those crons SAID; check 10 reads whether they said anything at all. Both
live in `longhorn_cron_evidence_logic.py`, split from `longhorn_backup_health_logic.py` when it
reached its module-length cap.

Split from `test_longhorn_backup_health.py`, which reached its own cap; the eight checks before
these stay there. The split is also the shape of the thing: these two read a journal rather than
the cluster, so their fixtures are log lines and nothing here needs the other file's clock
constants.

The two trim lines below are copied from `longhorn-trim-volumes.sh.j2` and the check matches them
as written. That is deliberate — a summary line's wording is the interface — so a reword there
fails these rather than silently blinding the check.

The transport that produces these lines is pinned separately by a real subprocess run in
`test_longhorn_backup_health_reader.py::test_reader_pins_the_journal_transport`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_health_cron_evidence.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import longhorn_cron_evidence_logic as logic


# The trim's own summary and abort lines, copied from longhorn-trim-volumes.sh.j2. They are
# matched as written, so a reword there breaks these rather than silently blinding the check.
_TRIM_CLEAN = "trimmed 41 volume(s), 3 skipped, 0 failed"
_TRIM_FAILED = "trimmed 38 volume(s), 3 skipped, 3 failed"
_TRIM_ABORT = "ABORT: no ready longhorn-manager pod on daniel-box"

# The one line a COMPLETED `b2-deletions` run always ends with, copied from
# `deletions_summary_line` in `scripts/diagnostics/probe_lib/b2_ledger.py` and matched as
# written for the same reason the trim's lines are.
_SUMMARY = "b2-deletions: charged 3, skipped 0, unpriced 0"


def test_cron_evidence_is_clean_on_a_successful_trim_and_a_priced_accounting():
    assert (
        logic.check_cron_evidence(
            [_TRIM_CLEAN],
            [_SUMMARY],
            26,
        )
        == []
    )


def test_cron_evidence_is_flagged_on_a_failed_trim():
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    problems = logic.check_cron_evidence([_TRIM_CLEAN, _TRIM_FAILED], [], 26)
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "failed on 3 volume(s)" in problems[0][1]


def test_cron_evidence_is_flagged_on_an_aborted_trim():
    problems = logic.check_cron_evidence([_TRIM_ABORT], [], 26)
    assert len(problems) == 1
    assert "aborted its last run" in problems[0][1]
    assert "no ready longhorn-manager pod" in problems[0][1]


def test_cron_evidence_reads_only_the_newest_trim_run():
    """A fixed trim clears on its next run — a failure two nights ago is not today's verdict."""
    assert (
        logic.check_cron_evidence([_TRIM_FAILED, _TRIM_ABORT, _TRIM_CLEAN], [], 26)
        == []
    )


def test_cron_evidence_is_flagged_on_an_unpriced_deletion_anywhere_in_the_window():
    """`logger` writes one entry per line, so the UNPRICED block is not the newest line."""
    lines = [
        "charged 1 deletion(s) over 26h: 12 Class C",
        "UNPRICED — these deletions spent Class C that cannot now be recovered:",
        "  n8n-files                backup-abc123",
        "  Their volume is absent from the block-tree snapshot, so nothing prices them.",
    ]
    problems = logic.check_cron_evidence([_TRIM_CLEAN], lines, 26)
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "UNPRICED" in problems[0][1]


def test_cron_evidence_reports_a_journal_read_that_failed():
    """None is "I could not look", which is not the same answer as an empty window."""
    problems = logic.check_cron_evidence(None, None, 26)
    assert [rank for rank, _ in problems] == [4, 4]
    assert "longhorn-trim journal" in problems[0][1]
    assert "b2-deletions journal" in problems[1][1]


def test_cron_evidence_is_silent_when_the_window_holds_nothing():
    """No reading is not a verdict. Whether the crons still run at all is a separate alarm."""
    assert logic.check_cron_evidence([], [], 26) == []


# ── check 10: are those two crons still firing at all? ──────────────────────────────────────

_NOW = 1_700_000_000.0
# Older than the 26h window, so a cron installed then has had a full window to fire in.
_INSTALLED_LONG_AGO = _NOW - 30 * 3600
_TRIM = logic.CronState(
    "longhorn-trim", "/etc/cron.d/longhorn-trim", _INSTALLED_LONG_AGO, False, True
)
_B2 = logic.CronState(
    "b2-deletions",
    "/etc/cron.d/b2-deletion-accounting",
    _INSTALLED_LONG_AGO,
    False,
    True,
)


def _liveness(trim_lines, deletion_lines, trim=_TRIM, deletion=_B2, window=26):
    return logic.check_cron_liveness(
        trim_lines, deletion_lines, trim, deletion, window, _NOW
    )


def test_cron_liveness_is_clean_when_both_crons_spoke_in_the_window():
    assert _liveness([_TRIM_CLEAN], [_SUMMARY]) == []


def test_cron_liveness_is_flagged_when_a_long_installed_cron_says_nothing():
    """The gap check 9 leaves open: an empty window reads as green to the content arm."""
    problems = _liveness([], [_SUMMARY])
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "longhorn-trim has logged nothing in the last 26h" in problems[0][1]
    assert "/etc/cron.d/longhorn-trim" in problems[0][1]


def test_cron_liveness_is_flagged_when_the_cron_entry_is_gone():
    """The issue's verify-by: remove the entry and the tile names the silent cron."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    gone = _TRIM._replace(installed_at=None)
    problems = _liveness([], [_SUMMARY], trim=gone)
    assert len(problems) == 1
    assert "the longhorn-trim cron is not installed" in problems[0][1]


def test_cron_liveness_is_clean_on_a_freshly_provisioned_host():
    """Installed inside the window, so it has not yet had a full period to fire in."""
    fresh = _TRIM._replace(installed_at=_NOW - 3600)
    assert _liveness([], [_SUMMARY], trim=fresh) == []


def test_cron_liveness_is_clean_for_a_cron_this_host_does_not_install():
    """`has_repo_checkout: false` never gets the B2-accounting cron — it must not page."""
    absent = _B2._replace(installed_at=None, expected=False)
    assert _liveness([_TRIM_CLEAN], [], deletion=absent) == []


def test_cron_liveness_separates_a_missing_cron_from_one_it_cannot_stat():
    """A missing cron and one it cannot stat have different fixes — the drill's lesson."""
    blind = _TRIM._replace(installed_at=None, unreadable=True)
    problems = _liveness([], [_SUMMARY], trim=blind)
    assert len(problems) == 1
    assert "could not stat /etc/cron.d/longhorn-trim" in problems[0][1]
    assert "not installed" not in problems[0][1]


def test_cron_liveness_does_not_double_report_a_journal_read_that_failed():
    """Check 9 already reports the unreadable journal; it is one fault, not two."""
    assert _liveness(None, None) == []


def test_cron_liveness_does_not_run_on_a_window_shorter_than_one_cron_period():
    """Below one period, silence is the normal reading — there is nothing to conclude."""
    assert _liveness([], [], window=12) == []


def test_cron_liveness_reads_trim_chatter_as_silence():
    """Only a line check 9 recognises counts: the trim's verdict has its own two shapes."""
    problems = _liveness(["some unrelated line under the tag"], [_SUMMARY])
    assert len(problems) == 1
    assert "longhorn-trim has logged nothing" in problems[0][1]


def test_cron_liveness_reads_b2_deletions_chatter_as_silence():
    """The #2545 gap: a run that logged a traceback and died used to read as alive.

    The cron pipes both streams through `logger`, so a traceback IS journal lines under the
    tag. Check 9 stays quiet on it too — a traceback carries no `UNPRICED` — so this arm is
    the only reader that can claim the run never finished.
    """
    traceback_lines = [
        "Traceback (most recent call last):",
        '  File "scripts/diagnostics/probe.py", line 1, in <module>',
        "ConnectionError: loki refused the connection",
    ]
    problems = _liveness([_TRIM_CLEAN], traceback_lines)
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "b2-deletions has logged nothing in the last 26h" in problems[0][1]


def test_cron_liveness_accepts_a_declined_b2_deletions_run():
    """A disarmed BackupTarget is a legitimate no-op, not a stopped cron."""
    declined = ["b2-deletions: declined: the B2 BackupTarget has no URL"]
    assert _liveness([_TRIM_CLEAN], declined) == []


def test_cron_liveness_accepts_the_summary_line_the_probe_writes():
    """The coupling test: the literal `probe.py b2-deletions` prints is what this matches.

    The shape lives in two trees with no shared import — the probe emits it, this check
    matches it — so nothing but a test holds the two together.
    """
    from diagnostics.probe_lib import b2_ledger

    assert _liveness([_TRIM_CLEAN], [b2_ledger.deletions_summary_line(3, 1, 0)]) == []
    assert (
        _liveness([_TRIM_CLEAN], [b2_ledger.deletions_declined_line("disarmed")]) == []
    )


def test_cron_liveness_reads_a_body_line_without_a_summary_as_silence():
    """#2565: the two shapes the legacy arm used to accept are body rows, not a verdict.

    `b2_ledger.py` still writes both — the priced total and the nothing-deleted row — but a run
    that dies after printing one of them never reaches `deletions_summary_line`. Accepting them
    kept the weaker "any line at all" reading alive for two line shapes.
    """
    body_rows = [
        "no new B2 backup deletions in the last 26h",
        "charged 3 deletion(s) over 26h: 186 Class C, priced from the listing",
    ]
    assert not logic.deletions_have_spoken(body_rows)
    problems = _liveness([_TRIM_CLEAN], body_rows)
    assert len(problems) == 1
    assert "b2-deletions has logged nothing in the last 26h" in problems[0][1]
