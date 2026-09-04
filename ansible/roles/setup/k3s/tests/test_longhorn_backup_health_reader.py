#!/usr/bin/env python3
"""The Longhorn backup-health reader run as a real subprocess, against stub `kubectl` scripts.

Split out of `test_longhorn_backup_health.py`, which keeps the pure decision arms. What only a
subprocess run can pin: env parsing, the argv shape host_lib.kubectl_runner builds, the
up/down<TAB>msg contract the shim's branches depend on, and the reader's own syslog line. The
stubs and the required-env builder are shared with the grace-cron suite in
`_longhorn_reader_stubs.py`.

`conftest.py` in this directory puts a stubbed `logger` first on PATH; the `logger_calls`
fixture is the file it appends to.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_health_reader.py
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from _longhorn_reader_stubs import (
    READER,
    _green_path_stub_kubectl,
    _reader_env,
    _rfc3339,
    _run_reader_against,
)


def test_reader_pins_the_transport(tmp_path):
    """Runs longhorn_backup_health.py as a real subprocess against a stub kubectl.

    The stub fails every call with a distinguishing marker, so this proves the reader shells out
    correctly (LONGHORN_BACKUP_KUBECTL, the namespace flag, env parsing) and still emits the
    up/down<TAB>msg contract the shim depends on — the part no pure-function test can see.

    LONGHORN_BACKUP_KUBECTL is given the stub's ABSOLUTE path rather than a bare name on PATH:
    host_lib.kubectl_runner prepends /usr/local/bin ahead of the caller's PATH, so a same-named
    stub elsewhere on PATH would be shadowed by a real kubectl on a host that has one.
    """
    stub = tmp_path / "stub-kubectl"
    stub.write_text("#!/usr/bin/env bash\necho 'STUB_KUBECTL_MARKER' >&2\nexit 1\n")
    stub.chmod(0o755)

    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL=str(stub))

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t")
    assert "STUB_KUBECTL_MARKER" in proc.stdout


def test_reader_syslog_line_is_intercepted(tmp_path, logger_calls):
    """The reader's own `logger` call reaches the conftest stub, not the host's syslog.

    This is the non-vacuity half of the autouse `_no_syslog` fixture: an empty `logger_calls`
    would mean either that the reader stopped logging its verdict, or that the real `logger`
    took the call — which is issue #1052, fixture verdicts (`STUB_KUBECTL_MARKER`, pytest tmp
    paths) shipped through Promtail into the Alert History board beside real ones.
    """
    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL="/bin/false")

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    lines = logger_calls.read_text().splitlines()
    assert any(
        line.startswith("-t longhorn-backup-health status=down") for line in lines
    ), lines


def test_reader_exits_nonzero_naming_a_missing_env_var(tmp_path):
    """A shim that stops exporting a var must be LOUD, not silently fall back to a stale constant.

    Every LONGHORN_* var is required (2026-09-04 review finding #3). This drops one from the
    otherwise-complete env and asserts the reader exits nonzero and names it — which is exactly
    what the shim's `if ! OUT=$(...)` / `[[ $RC -ne 0 ]]` branch turns into a `reader failed`
    push, rather than a wrong-but-plausible verdict computed from a hardcoded fallback.
    """
    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL="/bin/false")
    del env["LONGHORN_DAILY_BACKUP_BUDGET"]

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "LONGHORN_DAILY_BACKUP_BUDGET" in proc.stderr


def test_reader_treats_a_clean_bool_env_normally(tmp_path):
    """The clean half of the pair below: a recognized "False" must still disarm normally."""
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL="/bin/false",
        LONGHORN_R2_ARMED="False",
    )
    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_reader_exits_nonzero_on_an_unrecognized_bool_env(tmp_path):
    """An armed flag must REFUSE an unrecognized value, not fold it into False (2026-09-04
    review finding #9).

    `_require_bool_env` backs LONGHORN_BACKUP_ARMED and LONGHORN_R2_ARMED, and False there means
    DISARMED — a target's volumes get suppressed from every check rather than watched. A typo'd
    or truncated export used to fold silently into "disarmed" instead of failing loudly the way
    a missing var already does.
    """
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL="/bin/false",
        LONGHORN_R2_ARMED="maybe",
    )
    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "LONGHORN_R2_ARMED" in proc.stderr
    assert "maybe" in proc.stderr


def test_reader_green_path_pins_the_transport(tmp_path):
    """The clean half of test_reader_pins_the_transport: every query answered, verdict is UP.

    The red-path test above stubs kubectl to fail every call, which exercises the shell-out and
    the down<TAB>msg contract but never the eight checks' happy path — the jsonpath literals,
    the three row parsers, or the up<TAB>msg contract the shim's success branch depends on. This
    runs the reader against fixtures shaped to leave every one of the eight checks clean.
    """
    now = time.time()
    snapshot_ts = _rfc3339(now - 60)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    stub = _green_path_stub_kubectl(tmp_path, snapshot_ts)
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
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
    assert "backup target(s) default r2 available" in proc.stdout
    assert "1 backed-up volume(s) covered across daily+weekly" in proc.stdout
    assert "1 B2 backup(s)/24h (budget 16)" in proc.stdout


# ── fetch failures: the deadman must go DOWN with a reason, never quietly UP (issue #1061) ────
#
# test_reader_green_path_pins_the_transport above is the CLEAN half of every pair below: the same
# fixture, no knob set, verdict UP. Each test here turns exactly one fetch red and asserts the
# verdict flips and names that fetch — so a helper that stopped appending its problem, or one
# that started firing on a healthy answer, fails a test rather than reading green forever.
#
# The reader's OWN syslog line carries the full unranked problem list; stdout carries only the
# top-ranked one plus a count, so the fetch name is asserted against `logger_calls`.


@pytest.mark.parametrize(
    ("branch", "named"),
    [
        ("freshness", "backup freshness fetch failed (rc=124)"),
        ("errored-backups", "errored-backups fetch failed (rc=124)"),
        ("coverage", "backup coverage fetch failed (rc=124)"),
        ("tier-default", "daily tier volumes fetch failed (rc=124)"),
        ("recent", "recent backups fetch failed (rc=124)"),
        ("r2", "r2 volume set fetch failed (rc=124)"),
        ("failed-jobs", "failed-jobs fetch failed (rc=124)"),
    ],
)
def test_a_timed_out_fetch_is_flagged_by_name(tmp_path, logger_calls, branch, named):
    """rc 124 is host_lib's timeout code — the exact case that used to read as an empty result.

    Every one of these seven fetches turned a nonzero rc into `[]`/`set()` and fed it to a check
    that reads empty as clean: "nothing errored", "no failed jobs", or a tier silently dropped
    from the coverage count. A 30s API-server timeout on one call therefore left the whole
    verdict UP with a quietly smaller number in it.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_FAIL_BRANCH=branch)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert named in logged, logged


@pytest.mark.parametrize(
    ("branch", "named"),
    [
        ("errored-backups", "errored-backups fetch returned an unparseable body"),
        ("failed-jobs", "failed-jobs fetch returned an unparseable body"),
    ],
)
def test_an_unparseable_json_body_is_flagged_by_name(
    tmp_path, logger_calls, branch, named
):
    """A `null` body parses cleanly and carries no `items` — kubectl's answer on a truncated read.

    `json.loads("null")` returns None rather than raising, so the reader's old `except ValueError`
    never saw this one: it reached `.get("items")` as an AttributeError. Only the two `-o json`
    fetches are covered — for the five jsonpath fetches a garbage body is indistinguishable from
    data, and a malformed jsonpath makes kubectl exit nonzero, which the rc pair above covers.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_NULL_BRANCH=branch)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert named in logged, logged


def test_a_failed_coverage_fetch_does_not_cascade_into_the_tier_loop(
    tmp_path, logger_calls
):
    """One unread fetch reports one problem, not ten.

    Every tier is matched against the coverage rows, so passing the loop an empty list on a
    failed coverage fetch would report all nine tiers' volumes as stale or missing — burying the
    one thing that actually happened under nine consequences of it.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_FAIL_BRANCH="coverage")

    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert "backup coverage fetch failed" in logged, logged
    assert "tier volumes fetch failed" not in logged, logged
    assert "stale or missing" not in logged, logged
