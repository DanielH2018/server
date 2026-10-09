#!/usr/bin/env python3
"""Shared transport fixtures for the Longhorn backup-health reader's subprocess tests.

Not a test module — a helper the reader and grace-cron suites import. It holds the paths to the
reader and to the shared host_lib, the full required-env builder, and the three stub `kubectl`
scripts the subprocess tests run the reader against. They live here rather than in either suite
because both suites need them and pytest names test modules by basename repo-wide (there are no
`__init__.py` files), so one suite cannot import the other.

Consumers: `test_longhorn_backup_health_reader.py`, `test_longhorn_backup_grace_cron.py`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from lib.proc_testing import write_exec

READER = Path(__file__).resolve().parents[1] / "files" / "longhorn_backup_health.py"
HOST_LIB_DIR = Path(__file__).resolve().parents[2] / "common" / "files"

# The epoch every dated fixture here is measured from — the same one test_longhorn_backup_health
# pins. A fixture stamped `time.time() - age` would measure its age across the suite's own
# runtime, which is the straddle a pinned clock avoids.
NOW = 1_800_000_000.0

# Runs the reader with main(now=NOW) instead of `python longhorn_backup_health.py`, still as a
# subprocess: import-time env parsing, the sys.path bootstrap, the stub-kubectl shell-out and
# the up/down<TAB>msg contract are all the real ones. `-P` keeps the cwd off sys.path, so the
# bootstrap is what makes `logic` importable, as it is under the cron; `run_name` is not
# `__main__`, so the file's own entry line does not fire a second, clock-reading run. The one
# subprocess test that does not date a fixture (`test_reader_pins_the_transport`) keeps the
# bare `python <reader>` form, so the `__main__` line stays exercised.
_MAIN_WITH_NOW = (
    "import runpy, sys; "
    "g = runpy.run_path(sys.argv[1], run_name='longhorn_backup_health'); "
    "sys.exit(g['main'](now=float(sys.argv[2])))"
)


def reader_argv(now: float | None = None) -> list[str]:
    """The subprocess argv that runs the reader — bare, or through main(now=...)."""
    if now is None:
        return [sys.executable, str(READER)]
    return [sys.executable, "-P", "-c", _MAIN_WITH_NOW, str(READER), repr(now)]


def _reader_env(tmp_path, **overrides) -> dict:
    """Every LONGHORN_* env var the reader requires, with permissive defaults a test can override.

    Every one of these is REQUIRED by the reader (`_require_env` et al — no hardcoded fallback),
    so a subprocess test has to set all seventeen or the reader exits nonzero before doing
    anything else. Centralised here so each test only names the ONE
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
            # /bin/true — rc 0 with no output, which check 9 reads as "the window held nothing"
            # and is silent about. It must not be the real journalctl: the verdict would then
            # depend on whatever the machine running the suite happens to have logged. A test
            # about check 9 overrides this with /bin/false or a stub that prints lines.
            "LONGHORN_JOURNALCTL": "/bin/true",
            "LONGHORN_JOURNAL_TIMEOUT_S": "10",
            "LONGHORN_CRON_EVIDENCE_WINDOW_HOURS": "26",
            # Check 10's crons, installed an hour before NOW: the reader stats these paths, sees them
            # inside the window, and grants the freshly-provisioned grace — so the empty
            # journal above stays silent and the green path stays green. A test about check 10
            # overrides a path with one that does not exist, or backdates its mtime further.
            "LONGHORN_TRIM_CRON_FILE": str(_touch_cron(tmp_path, "longhorn-trim")),
            "LONGHORN_TRIM_CRON_EXPECTED": "True",
            "LONGHORN_B2_DELETIONS_CRON_FILE": str(
                _touch_cron(tmp_path, "b2-deletion-accounting")
            ),
            "LONGHORN_B2_DELETIONS_CRON_EXPECTED": "True",
            # No fire stamps: neither cron has fired, so the install-time grace above decides.
            "LONGHORN_TRIM_CRON_FIRED_STAMP": str(tmp_path / "fired" / "longhorn-trim"),
            "LONGHORN_B2_DELETIONS_CRON_FIRED_STAMP": str(
                tmp_path / "fired" / "b2-deletion-accounting"
            ),
        }
    )
    env.update(overrides)
    return env


def _touch_cron(tmp_path, name: str) -> Path:
    """A stand-in for `/etc/cron.d/<name>`, installed an hour before the pinned clock.

    Its mtime is set against NOW rather than left at the real wall clock: the subprocess
    tests pin `now` to NOW, and a file stamped with today's date would read to check 10 as
    installed months before that and page for a cron it was handed no journal for.
    """
    path = tmp_path / "cron.d" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (NOW - 3600, NOW - 3600))
    return path


def _rfc3339(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _backup_list_body(*backups: tuple[str, str, str]) -> str:
    """`kubectl get backups.longhorn.io -o json`: one Completed backup per (volume, ts, job)."""
    items = [
        {
            "metadata": {"name": f"backup-{volume}"},
            "status": {
                "volumeName": volume,
                "snapshotCreatedAt": snapshot_ts,
                "state": "Completed",
                "size": "1048576",
                "labels": {"RecurringJob": job},
            },
        }
        for volume, snapshot_ts, job in backups
    ]
    return json.dumps({"items": items})


def _green_path_stub_kubectl(tmp_path, snapshot_ts: str) -> Path:
    """A stub kubectl answering every query the reader's green path issues, from fixtures.

    Dispatches on argv (after stripping the `-n <namespace>` host_lib.kubectl_runner inserts),
    not on raw text matching, so it stays exact even though several distinct queries all target
    `volumes.longhorn.io` with different -o jsonpath shapes. The backup list is one `-o json`
    fetch (#3735); any other backup query is an UNEXPECTED ARGS failure. Two volumes:
    `pvc-web-data` in the daily tier and `pvc-weekly-data` in weekly shard d4, each with one
    backup from its own job. Every other tier's label selector matches nothing.

    The RecurringJob pod-log read answers `STUB_POD_LOGS` (empty by default), and only to the
    exact flags the reader passes: without `--tail=-1` kubectl returns 10 lines per pod, and a
    reader that dropped it would pass every test while reading nothing in production.

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
BACKUP_LIST = __BACKUP_LIST__

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]

if args[:2] == ["get", "backuptarget"]:
    branch, body = "target-" + args[2], "true"
elif args == ["get", "backups.longhorn.io", "-o", "json"]:
    branch, body = "backups", BACKUP_LIST
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    branch, body = "failed-jobs", '{"items": []}'
elif args == [
    "logs", "-l", "recurring-job.longhorn.io", "--prefix", "--tail=-1", "--ignore-errors"
]:
    branch, body = "pod-logs", os.environ.get("STUB_POD_LOGS", "")
elif args[:2] == ["get", "volumes.longhorn.io"] and "-l" in args:
    sel = args[args.index("-l") + 1]
    branch = "tier-" + sel.split("/")[-1].split("=")[0]
    if sel == "recurring-job-group.longhorn.io/default=enabled":
        body = "pvc-web-data %s default/web-data default\n" % SNAPSHOT_TS
    elif sel == "recurring-job-group.longhorn.io/weekly-backup-d4=enabled":
        body = "pvc-weekly-data %s default/weekly-data default\n" % SNAPSHOT_TS
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
""".replace("__SNAPSHOT_TS__", snapshot_ts).replace(
        "__BACKUP_LIST__",
        repr(
            _backup_list_body(
                ("pvc-web-data", snapshot_ts, "daily-backup"),
                ("pvc-weekly-data", snapshot_ts, "weekly-backup-d4"),
            )
        ),
    )
    return write_exec(stub, script)


def _run_reader_against(stub, tmp_path, **env_overrides):
    """Run the reader against `stub` on an otherwise-green fixture, returning the finished proc.

    The reader runs with main(now=NOW), so the drill stamp written here and the snapshot stamp
    the caller baked into `stub` are both judged against NOW.
    """
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir(exist_ok=True)
    (drill_dir / "last-success").write_text(str(int(NOW - 3600)))
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        **env_overrides,
    )
    return subprocess.run(
        reader_argv(now=NOW),
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
    backup_list = _backup_list_body(("pvc-old", old_backup_ts, "daily-backup"))
    script = f"""#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]


def emit(text, rc=0):
    sys.stdout.write(text)
    sys.exit(rc)


if args[:3] == ["get", "backuptarget", "default"]:
    emit("true")
elif args == ["get", "backups.longhorn.io", "-o", "json"]:
    emit({backup_list!r})
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    emit('{{"items": []}}')
elif args[:1] == ["logs"]:
    emit("")
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
    return write_exec(stub, script)
