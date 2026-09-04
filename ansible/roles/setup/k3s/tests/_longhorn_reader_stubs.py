#!/usr/bin/env python3
"""Shared transport fixtures for the Longhorn backup-health reader's subprocess tests.

Not a test module — a helper the reader and grace-cron suites import. It holds the paths to the
reader and to the shared host_lib, the full required-env builder, and the three stub `kubectl`
scripts the subprocess tests run the reader against. They live here rather than in either suite
because both suites need them and pytest names test modules by basename repo-wide (there are no
`__init__.py` files), so one suite cannot import the other.

Consumers: `test_longhorn_backup_health_reader.py`, `test_longhorn_backup_grace_cron.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

READER = Path(__file__).resolve().parents[1] / "files" / "longhorn_backup_health.py"
HOST_LIB_DIR = Path(__file__).resolve().parents[2] / "common" / "files"


def _reader_env(tmp_path, **overrides) -> dict:
    """Every LONGHORN_* env var the reader requires, with permissive defaults a test can override.

    Every one of these is REQUIRED by the reader (`_require_env` et al — no hardcoded fallback,
    the 2026-09-04 review's finding #3), so a subprocess test that used to set only a couple of
    vars and rely on module-level defaults for the rest now has to set all thirteen or the reader
    exits nonzero before doing anything else. Centralised here so each test only names the ONE
    var it cares about overriding. LONGHORN_BACKUP_KUBECTL is the exception: every subprocess
    test points it at its own stub, so it has no default here.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{HOST_LIB_DIR}:{env.get('PYTHONPATH', '')}"
    env["LONGHORN_RESTORE_DRILL_STAMP_DIR"] = str(tmp_path / "no-such-drill-dir")
    env.update(
        {
            "LONGHORN_BACKUP_NAMESPACE": "longhorn-system",
            "LONGHORN_BACKUP_KUBECTL_TIMEOUT_S": "30",
            "LONGHORN_BACKUP_ARMED": "True",
            "LONGHORN_R2_ARMED": "True",
            "LONGHORN_BACKUP_MAX_AGE_HOURS": "30",
            "LONGHORN_WEEKLY_BACKUP_MAX_AGE_HOURS": "198",
            "LONGHORN_BACKUP_ERROR_MAX_AGE_HOURS": "24",
            "LONGHORN_DAILY_BACKUP_BUDGET": "16",
            "LONGHORN_BACKUP_CRON": "30 3 * * *",
            "LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR": "30 4",
            "LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS": "3",
            "LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS": "5",
        }
    )
    env.update(overrides)
    return env


def _rfc3339(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _green_path_stub_kubectl(tmp_path, snapshot_ts: str) -> Path:
    """A stub kubectl answering every query the reader's green path issues, from fixtures.

    Dispatches on argv (after stripping the `-n <namespace>` host_lib.kubectl_runner inserts),
    not on raw text matching, so it stays exact even though several distinct queries all target
    `backups.longhorn.io`/`volumes.longhorn.io` with different -o jsonpath shapes. One volume,
    `pvc-web-data`, is backed up by the "daily" tier only — every other tier's label selector
    matches nothing, which is the ordinary (and simplest-to-fixture) shape for a fleet where only
    one recurring job is armed.

    Each dispatch arm carries a branch NAME, and two env knobs turn one named branch red without
    disturbing the other eight: `STUB_FAIL_BRANCH` makes it exit 124 (host_lib's timeout code)
    and `STUB_NULL_BRANCH` makes it answer the JSON literal `null` with rc 0. That is what lets
    the fetch-failure tests below reuse this one fixture instead of shipping a stub per fetch.
    """
    stub = tmp_path / "stub-kubectl-green"
    script = r"""#!/usr/bin/env python3
import os
import sys

SNAPSHOT_TS = "__SNAPSHOT_TS__"

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]
joined = " ".join(args)

if args[:2] == ["get", "backuptarget"]:
    branch, body = "target-" + args[2], "true"
elif args[:2] == ["get", "backups.longhorn.io"] and args[-1] == "json":
    branch, body = "errored-backups", '{"items": []}'
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    branch, body = "failed-jobs", '{"items": []}'
elif args[:2] == ["get", "backups.longhorn.io"] and "|" in joined:
    branch = "coverage"
    body = "pvc-web-data|%s|daily-backup\n" % SNAPSHOT_TS
elif args[:2] == ["get", "backups.longhorn.io"] and "size" in joined:
    branch = "recent"
    body = "pvc-web-data %s 1048576\n" % SNAPSHOT_TS
elif args[:2] == ["get", "backups.longhorn.io"] and "snapshotCreatedAt" in joined:
    branch, body = "freshness", "%s\n" % SNAPSHOT_TS
elif args[:2] == ["get", "volumes.longhorn.io"] and "-l" in args:
    sel = args[args.index("-l") + 1]
    branch = "tier-" + sel.split("/")[-1].split("=")[0]
    if sel == "recurring-job-group.longhorn.io/default=enabled":
        body = "pvc-web-data %s default/web-data default\n" % SNAPSHOT_TS
    else:
        body = ""
elif args[:2] == ["get", "volumes.longhorn.io"]:
    branch, body = "r2", ""
else:
    sys.stderr.write("UNEXPECTED ARGS: %r\n" % (args,))
    sys.exit(1)

if branch == os.environ.get("STUB_FAIL_BRANCH"):
    sys.stderr.write("STUB_FETCH_FAILED %s\n" % branch)
    sys.exit(124)
if branch == os.environ.get("STUB_NULL_BRANCH"):
    body = "null"

sys.stdout.write(body)
""".replace("__SNAPSHOT_TS__", snapshot_ts)
    stub.write_text(script)
    stub.chmod(0o755)
    return stub


def _run_reader_against(stub, tmp_path, **env_overrides):
    """Run the reader against `stub` on an otherwise-green fixture, returning the finished proc."""
    now = time.time()
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir(exist_ok=True)
    (drill_dir / "last-success").write_text(str(int(now - 3600)))
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        **env_overrides,
    )
    return subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _grace_pair_stub_kubectl(tmp_path, created_ts: str, old_backup_ts: str) -> Path:
    """Two daily-tier volumes: `pvc-old` already backed up, `pvc-new` created moments ago.

    `pvc-old` has a matching coverage row so checks 2/3/5/6 stay clean regardless of the cron
    parse — only `pvc-new`'s fate (graced silently vs. paged as uncovered) depends on whether
    LONGHORN_BACKUP_CRON parses.
    """
    stub = tmp_path / "stub-kubectl-grace"
    script = f"""#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]
joined = " ".join(args)


def emit(text, rc=0):
    sys.stdout.write(text)
    sys.exit(rc)


if args[:3] == ["get", "backuptarget", "default"]:
    emit("true")
elif args[:2] == ["get", "backups.longhorn.io"] and args[-1] == "json":
    emit('{{"items": []}}')
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    emit('{{"items": []}}')
elif args[:2] == ["get", "backups.longhorn.io"] and "|" in joined:
    emit("pvc-old|{old_backup_ts}|daily-backup\\n")
elif args[:2] == ["get", "backups.longhorn.io"] and "size" in joined:
    emit("pvc-old {old_backup_ts} 1048576\\n")
elif args[:2] == ["get", "backups.longhorn.io"] and "snapshotCreatedAt" in joined:
    emit("{old_backup_ts}\\n")
elif args[:2] == ["get", "volumes.longhorn.io"] and "-l" in args:
    sel = args[args.index("-l") + 1]
    if sel == "recurring-job-group.longhorn.io/default=enabled":
        emit(
            "pvc-old {old_backup_ts} default/old-data default\\n"
            "pvc-new {created_ts} default/new-data default\\n"
        )
    else:
        emit("")
elif args[:2] == ["get", "volumes.longhorn.io"]:
    emit("")
else:
    sys.stderr.write("UNEXPECTED ARGS: %r\\n" % (args,))
    sys.exit(1)
"""
    stub.write_text(script)
    stub.chmod(0o755)
    return stub
