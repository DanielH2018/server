#!/usr/bin/env python3
"""The first-run grace pair: whether LONGHORN_BACKUP_CRON parses decides a new volume's fate.

Split out of `test_longhorn_backup_health.py`, which keeps the pure decision arms. These two are
the `_is_clean`/`_is_flagged` pair for one behaviour and stay together: a well-formed cron graces
a volume created moments ago, and a malformed one degrades to no grace and pages it — rather than
raising at module scope and taking down all eight checks, which is what it did before the
2026-09-04 review's finding #4. Both run the reader for real, because the bug was in the reader's
import-time work rather than in the pure core.

The stub `kubectl` and the required-env builder are shared with the reader suite in
`_longhorn_reader_stubs.py`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_grace_cron.py
"""

from __future__ import annotations

import subprocess
import sys
import time

from _longhorn_reader_stubs import (
    READER,
    _grace_pair_stub_kubectl,
    _reader_env,
    _rfc3339,
)


def test_malformed_cron_pages_the_new_volume_instead_of_gracing_it(tmp_path):
    """FLAGGED half: a malformed LONGHORN_BACKUP_CRON used to raise IndexError before main() ran.

    _hhmm_from_two_field_cron() executed at module scope, so a bad value took down all eight
    checks at once, not just the daily tier's first-run grace (2026-09-04 review finding #4).
    With the fix, a malformed value degrades to `first_run_after(..., None, ...)`, which
    check_tier() already treats as "no grace" — `pvc-new`, created moments ago, is paged as
    uncovered instead of silently excused, and the reader still completes and emits a verdict.
    """
    now = time.time()
    old_ts = _rfc3339(now - 3600)
    new_ts = _rfc3339(now - 30)
    stub = _grace_pair_stub_kubectl(tmp_path, created_ts=new_ts, old_backup_ts=old_ts)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_R2_ARMED="False",
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        LONGHORN_BACKUP_CRON="30",  # malformed: one field, no hour
    )

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "pvc-new" in proc.stdout or "new-data" in proc.stdout, proc.stdout


def test_well_formed_cron_graces_the_new_volume(tmp_path):
    """CLEAN half of the malformed-cron pair: a well-formed value grants the new-volume grace."""
    now = time.time()
    old_ts = _rfc3339(now - 3600)
    new_ts = _rfc3339(now - 30)
    stub = _grace_pair_stub_kubectl(tmp_path, created_ts=new_ts, old_backup_ts=old_ts)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_R2_ARMED="False",
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        LONGHORN_BACKUP_CRON="30 3 * * *",
    )

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("up\t"), proc.stdout
    assert "awaiting their first scheduled backup" in proc.stdout, proc.stdout
