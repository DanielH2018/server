#!/usr/bin/env python3
"""Longhorn backup-plane heartbeat — the cluster-reading half of longhorn-backup-health.sh.

Reads the cluster through the read-only ServiceAccount kubeconfig and the restore-drill's stamp
files on disk, then prints one tab-separated line, `up<TAB>msg` or `down<TAB>msg`, for the
wrapper to log and push to Kuma. It never exits nonzero on a bad verdict: a DOWN is data to
report, not a crash. Same shape as configarr_health.py: this file does the I/O,
longhorn_backup_health_logic.py holds every threshold and decides up-or-down, and the split is
what makes the decisions unit-testable without a cluster.

Runs on daniel-box via `uv run --no-project --python <pin>` (host_python_version in
ansible/inventory/group_vars/all.yml). The Kuma push token stays in the templated wrapper, which
sources it from an 0640 env file at runtime, and never appears here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
import longhorn_backup_health_logic as logic

NAMESPACE = os.environ.get("LONGHORN_BACKUP_NAMESPACE", "longhorn-system")
KUBECTL_BIN = os.environ.get("LONGHORN_BACKUP_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("LONGHORN_BACKUP_KUBECTL_TIMEOUT_S", "30"))

kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, TIMEOUT)


def _require_env(name: str) -> str:
    """Value of a required, shim-templated env var — raises SystemExit naming it if unset.

    Every LONGHORN_* var read below is unconditionally exported by
    longhorn-backup-health.sh.j2; there is no value for a Longhorn threshold this reader could
    make up that is safer than refusing to guess. A hardcoded fallback here used to mean a
    template edit that dropped one `export` line, or an install that skipped the copy, would
    silently substitute a stale or wrong constant instead of the shim's actual setting — a wrong
    verdict with nothing in the output naming the cause. Exiting nonzero instead makes the shim's
    already-existing "reader failed" path (longhorn-backup-health.sh.j2) report it, which names
    the missing variable rather than pushing a plausible-looking but silently-wrong UP or DOWN.
    """
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(
            f"required env var {name} is not set (should be exported by the shim)"
        )
    return value


def _require_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"env var {name}={raw!r} is not an integer") from None


def _require_bool_env(name: str) -> bool:
    """Parses a Jinja `{{ var | bool }}` render, which Python's str() spells "True"/"False"."""
    return _require_env(name).strip().lower() in ("true", "1", "yes")


def _hhmm_from_two_field_cron(spec: str) -> str | None:
    """`MINUTE HOUR ...` -> `H:MM`, matching the shell's `awk '{ print $2 ":" $1 }'`.

    Works for both a full 5-field cron ("30 3 * * *") and the weekly minute-hour pair
    ("30 4") — both put minute first and hour second. Returns None on fewer than two fields
    rather than raising: awk's own `$2`/`$1` on a short line silently prints an empty field, no
    exit, so the shell only ever lost the first-run grace projection on a malformed value —
    never the whole heartbeat. Called from main(), not module scope: an IndexError here used to
    fire before any of the other seven checks ran, taking the whole reader down over one bad
    schedule string. logic.first_run_after() treats None the same way it treats any other
    unparseable hhmm — the grace is disabled for that tier, not the reader.
    """
    fields = spec.split()
    if len(fields) < 2:
        return None
    minute, hour = fields[0], fields[1]
    return f"{hour}:{minute}"


BACKUP_ARMED = _require_bool_env("LONGHORN_BACKUP_ARMED")
R2_ARMED = _require_bool_env("LONGHORN_R2_ARMED")
MAX_AGE_HOURS = _require_int_env("LONGHORN_BACKUP_MAX_AGE_HOURS")
WEEKLY_MAX_AGE_HOURS = _require_int_env("LONGHORN_WEEKLY_BACKUP_MAX_AGE_HOURS")
ERROR_MAX_AGE_HOURS = _require_int_env("LONGHORN_BACKUP_ERROR_MAX_AGE_HOURS")
DAILY_BACKUP_BUDGET = _require_int_env("LONGHORN_DAILY_BACKUP_BUDGET")
BACKUP_CRON = _require_env("LONGHORN_BACKUP_CRON")
WEEKLY_BACKUP_MINUTE_HOUR = _require_env("LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR")
DRILL_STAMP_DIR = _require_env("LONGHORN_RESTORE_DRILL_STAMP_DIR")
DRILL_MAX_AGE_DAYS = _require_int_env("LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS")
DRILL_COVERAGE_SLACK_DAYS = _require_int_env(
    "LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS"
)

MAX_AGE_S = MAX_AGE_HOURS * 3600
WEEKLY_MAX_AGE_S = WEEKLY_MAX_AGE_HOURS * 3600


def _read_stamp(path: str) -> tuple[str | None, bool]:
    """(content, unreadable) — content's trailing newlines stripped, matching bash `$(cat ...)`.

    `unreadable` is True only when the file EXISTS but couldn't be opened (permissions, a
    directory in its place, ...) — distinct from FileNotFoundError, which is the ordinary
    "never written" case and returns `(None, False)`. Bash's own `[[ -r ]]` made this same
    distinction, and it matters: on 2026-08-19 the stamp directory's mode made a FRESH,
    successful drill's stamp unreadable by this script's user, and a checker that can't tell
    "never ran" from "ran, but I can't read the proof" reports the wrong one — "no restore drill
    has ever succeeded", permanently, while a drill had just in fact succeeded. See
    check_restore_drill()'s docstring in the logic module for how this is consumed.
    """
    try:
        with open(path) as fh:
            return fh.read().rstrip("\n"), False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def _parse_volume_rows(raw: str) -> list[tuple[str, str, str, str]]:
    """(volume, creationTimestamp, namespace/pvcName, backupTargetName) per non-blank line."""
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 3)
        parts += [""] * (4 - len(parts))
        vol, created, claim, target = parts[:4]
        if not vol:
            continue
        rows.append((vol, created, claim, target))
    return rows


def _parse_pipe_rows(raw: str) -> list[tuple[str, str, str]]:
    """(volumeName, snapshotCreatedAt, RecurringJob) per non-blank `|`-delimited line."""
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        parts += [""] * (3 - len(parts))
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _parse_space_rows(raw: str) -> list[tuple[str, str, str]]:
    """(volumeName, snapshotCreatedAt, size) per non-blank space-delimited line."""
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 2)
        parts += [""] * (3 - len(parts))
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _fetch_target(name: str) -> dict:
    """{"raw": <.status.available, incl. any error text>, "reason": <conditions[0].message>}."""
    # Mirrors `AVAILABLE=$(... 2>&1)`: the raw text is used whatever the exit code, since a
    # kubectl failure IS the "unavailable" evidence here.
    _availability_rc, raw = kubectl(
        "get", "backuptarget", name, "-o", "jsonpath={.status.available}"
    )
    if raw == "true":
        # Matches the original: the reason lookup only runs when the target is NOT reporting
        # available. logic.check_target_availability() never reads "reason" on the available
        # path, so fetching it there was a wasted kubectl call — halving this check's calls on a
        # healthy tick (2 armed targets x 2 calls -> 2 targets x 1 call).
        return {"raw": raw, "reason": ""}
    # The reason lookup mirrors `2>/dev/null`: a failure here is discarded, not folded in.
    reason_rc, reason = kubectl(
        "get", "backuptarget", name, "-o", "jsonpath={.status.conditions[0].message}"
    )
    reason = reason[:160] if reason_rc == 0 else ""
    return {"raw": raw, "reason": reason}


def _fetch_backups_json() -> list[dict]:
    rc, out = kubectl("get", "backups.longhorn.io", "-o", "json")
    if rc != 0:
        return []
    try:
        return json.loads(out).get("items", [])
    except ValueError:
        return []


def _fetch_jobs_json() -> list[dict]:
    rc, out = kubectl("get", "jobs.batch", "-o", "json")
    if rc != 0:
        return []
    try:
        return json.loads(out).get("items", [])
    except ValueError:
        return []


def _fetch_r2_volumes() -> set[str]:
    rc, out = kubectl(
        "get",
        "volumes.longhorn.io",
        "-o",
        """jsonpath={range .items[?(@.spec.backupTargetName=="r2")]}{.metadata.name}{" "}{end}""",
    )
    if rc != 0:
        return set()
    return set(out.split())


def _fetch_drill_candidates() -> list[str]:
    path = os.path.join(DRILL_STAMP_DIR, "candidates")
    try:
        with open(path) as fh:
            return [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return []


def _fetch_drill_seen(candidates: list[str]) -> dict[str, float]:
    seen = {}
    for cand in candidates:
        try:
            seen[cand] = os.stat(os.path.join(DRILL_STAMP_DIR, "seen", cand)).st_mtime
        except OSError:
            continue
    return seen


def _fetch_drill_success(candidates: list[str]) -> dict[str, str]:
    # check 8 only needs "did this candidate ever succeed" per volume, not why one didn't — the
    # missing-vs-unreadable distinction (check 7's fix, above) doesn't carry an extra message
    # here, so the second element is discarded.
    success = {}
    for cand in candidates:
        content, _unreadable = _read_stamp(
            os.path.join(DRILL_STAMP_DIR, "success", cand)
        )
        if content is not None:
            success[cand] = content
    return success


def _syslog(message: str) -> None:
    """Writes one line to syslog via `logger`, never raising and never touching our own stdio.

    stdout/stderr are explicitly sent to DEVNULL rather than inherited. The wrapper captures
    this reader as `OUT=$(... 2>"$ERR")`; if `logger` itself wrote anything to its own stdout or
    stderr (a missing /dev/log socket, e.g.), inheriting our fds would put that text into the
    same stream the wrapper reads the verdict from — the contamination path the 2026-09-04
    review found (`logger: socket /dev/log: ...` turning an `up` into a false DOWN). check=False
    only silences logger's own nonzero exit; a missing `logger` binary raises OSError, caught
    here too, since neither failure may take this reader down over its own logging call.
    """
    try:
        subprocess.run(
            ["logger", "-t", "longhorn-backup-health", message],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def main() -> int:
    now_s = time.time()

    # Parsed here, not at module scope: a malformed value must degrade the first-run grace for
    # the affected tier(s), not crash the reader before any of the other seven checks run (see
    # _hhmm_from_two_field_cron's docstring — the 2026-09-04 review's finding #4).
    daily_run_hhmm = _hhmm_from_two_field_cron(BACKUP_CRON)
    if daily_run_hhmm is None:
        _syslog(
            f"malformed LONGHORN_BACKUP_CRON={BACKUP_CRON!r}; "
            "disabling the daily tier's first-run grace"
        )
    weekly_run_hhmm = _hhmm_from_two_field_cron(WEEKLY_BACKUP_MINUTE_HOUR)
    if weekly_run_hhmm is None:
        _syslog(
            f"malformed LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR={WEEKLY_BACKUP_MINUTE_HOUR!r}; "
            "disabling the weekly tiers' first-run grace"
        )

    # ── targets (armed only) and disarmed set — same order Jinja renders BACKUP_TARGETS in.
    backup_targets: list[str] = []
    disarmed_targets: list[str] = []
    (backup_targets if BACKUP_ARMED else disarmed_targets).append("default")
    (backup_targets if R2_ARMED else disarmed_targets).append("r2")

    problems: list[tuple[int, str]] = []
    problems += logic.check_target_availability(
        {name: _fetch_target(name) for name in backup_targets}
    )

    # ── check 2: freshness ────────────────────────────────────────────────────────────────
    rc, backups_jsonpath = kubectl(
        "get",
        "backups.longhorn.io",
        "-o",
        'jsonpath={range .items[*]}{.status.snapshotCreatedAt}{"\\n"}{end}',
    )
    newest_ts = None
    if rc == 0:
        stamps = [line for line in backups_jsonpath.splitlines() if line.strip()]
        newest_ts = max(stamps) if stamps else None
    fresh_problem, age_s = logic.check_freshness(
        newest_ts, now_s, MAX_AGE_S, MAX_AGE_HOURS
    )
    if fresh_problem:
        problems.append(fresh_problem)

    # ── check 3: errored backups ──────────────────────────────────────────────────────────
    error_cutoff_s = now_s - ERROR_MAX_AGE_HOURS * 3600
    backup_items = _fetch_backups_json()
    errored_problem = logic.check_errored_backups(
        backup_items, error_cutoff_s, ERROR_MAX_AGE_HOURS
    )
    if errored_problem:
        problems.append(errored_problem)

    # ── check 4: per-tier coverage ────────────────────────────────────────────────────────
    rc, coverage_raw = kubectl(
        "get",
        "backups.longhorn.io",
        "-o",
        'jsonpath={range .items[*]}{.status.volumeName}{"|"}{.status.snapshotCreatedAt}'
        '{"|"}{.status.labels.RecurringJob}{"\\n"}{end}',
    )
    coverage_rows = _parse_pipe_rows(coverage_raw) if rc == 0 else []

    disarmed_set = set(disarmed_targets)
    result = logic.TierResult()
    tiers = [
        (
            "recurring-job-group.longhorn.io/default=enabled",
            MAX_AGE_S,
            "daily",
            "daily-backup",
            daily_run_hhmm,
            "*",
        ),
        *[
            (
                f"recurring-job-group.longhorn.io/weekly-backup-d{shard}=enabled",
                WEEKLY_MAX_AGE_S,
                f"weekly-d{shard}",
                f"weekly-backup-d{shard}",
                weekly_run_hhmm,
                str(shard),
            )
            for shard in range(7)
        ],
        (
            "recurring-job-group.longhorn.io/weekly-backup=enabled",
            WEEKLY_MAX_AGE_S,
            "weekly-legacy",
            "__no_job__",
            weekly_run_hhmm,
            "*",
        ),
    ]
    for selector, max_age_s, tier, job, run_hhmm, dow in tiers:
        rc, rows_raw = kubectl(
            "get",
            "volumes.longhorn.io",
            "-l",
            selector,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{" "}{.metadata.creationTimestamp}'
            '{" "}{.status.kubernetesStatus.namespace}/{.status.kubernetesStatus.pvcName}'
            '{" "}{.spec.backupTargetName}{"\\n"}{end}',
        )
        rows = _parse_volume_rows(rows_raw) if rc == 0 else []
        logic.check_tier(
            result,
            rows,
            coverage_rows,
            max_age_s,
            tier,
            job,
            run_hhmm,
            dow,
            disarmed_set,
            now_s,
        )

    uncovered_problem = logic.uncovered_problem(result)
    if uncovered_problem:
        problems.append(uncovered_problem)

    # ── check 5: recent-backup budget ─────────────────────────────────────────────────────
    day_ago_s = now_s - 86400
    rc, recent_raw = kubectl(
        "get",
        "backups.longhorn.io",
        "-o",
        'jsonpath={range .items[*]}{.status.volumeName}{" "}{.status.snapshotCreatedAt}'
        '{" "}{.status.size}{"\\n"}{end}',
    )
    recent_rows = _parse_space_rows(recent_raw) if rc == 0 else []
    r2_volumes = _fetch_r2_volumes()
    recent_n, recent_bytes = logic.compute_recent_backups(
        recent_rows, r2_volumes, day_ago_s
    )
    budget_problem = logic.check_recent_budget(
        recent_n, recent_bytes, DAILY_BACKUP_BUDGET
    )
    if budget_problem:
        problems.append(budget_problem)

    # ── check 6: failed jobs ──────────────────────────────────────────────────────────────
    job_items = _fetch_jobs_json()
    failed_jobs_problem = logic.check_failed_jobs(
        job_items, error_cutoff_s, ERROR_MAX_AGE_HOURS
    )
    if failed_jobs_problem:
        problems.append(failed_jobs_problem)

    # ── check 7: restore drill freshness ──────────────────────────────────────────────────
    drill_stamp_path = os.path.join(DRILL_STAMP_DIR, "last-success")
    drill_content, drill_unreadable = _read_stamp(drill_stamp_path)
    drill_max_age_s = DRILL_MAX_AGE_DAYS * 86400
    drill_problem = logic.check_restore_drill(
        drill_content,
        drill_stamp_path,
        now_s,
        drill_max_age_s,
        DRILL_MAX_AGE_DAYS,
        stamp_unreadable=drill_unreadable,
    )
    if drill_problem:
        problems.append(drill_problem)

    # ── check 8: restore-drill rotation coverage ──────────────────────────────────────────
    candidates = _fetch_drill_candidates()
    seen = _fetch_drill_seen(candidates)
    success = _fetch_drill_success(candidates)
    coverage_problem = logic.check_restore_coverage(
        candidates, seen, success, now_s, DRILL_COVERAGE_SLACK_DAYS
    )
    if coverage_problem:
        problems.append(coverage_problem)

    status, msg, push_msg = logic.build_verdict(
        problems,
        backup_targets=backup_targets,
        disarmed_targets=disarmed_targets,
        age_s=age_s,
        checked=result.checked,
        recent_n=recent_n,
        daily_backup_budget=DAILY_BACKUP_BUDGET,
        suppressed=result.suppressed,
        graced_new=result.graced_new,
        graced_new_vols=result.graced_new_vols,
        graced_seeded=result.graced_seeded,
        graced_seeded_vols=result.graced_seeded_vols,
    )

    # Log the FULL verdict before printing the ranked one, unconditionally — matching the
    # original script's "log before push" ordering, so a DOWN status logged here still leaves a
    # journalctl trail even though only `push_msg` reaches the wrapper's stdout/Kuma.
    _syslog(f"status={status} {msg}")

    print("%s\t%s" % (status, push_msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
