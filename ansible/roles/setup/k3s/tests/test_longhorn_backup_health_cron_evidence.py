#!/usr/bin/env python3
"""Executing tests for check 9 — whether the trim and B2-accounting crons have a reader.

Split from `test_longhorn_backup_health.py`, which reached its module-length cap; the eight
checks before this one stay there. The split is also the shape of the thing: check 9 reads a
journal rather than the cluster, so its fixtures are log lines and nothing here needs the other
file's clock constants.

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
import longhorn_backup_health_logic as logic


# The trim's own summary and abort lines, copied from longhorn-trim-volumes.sh.j2. They are
# matched as written, so a reword there breaks these rather than silently blinding the check.
_TRIM_CLEAN = "trimmed 41 volume(s), 3 skipped, 0 failed"
_TRIM_FAILED = "trimmed 38 volume(s), 3 skipped, 3 failed"
_TRIM_ABORT = "ABORT: no ready longhorn-manager pod on daniel-box"


def test_cron_evidence_is_clean_on_a_successful_trim_and_a_priced_accounting():
    assert (
        logic.check_cron_evidence(
            [_TRIM_CLEAN],
            ["charged 3 deletion(s) over 26h: 186 Class C, priced from the listing"],
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
