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
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
import longhorn_backup_health_logic as logic

NAMESPACE = os.environ.get("LONGHORN_BACKUP_NAMESPACE", "longhorn-system")
KUBECTL_BIN = os.environ.get("LONGHORN_BACKUP_KUBECTL", "k3s kubectl")
TIMEOUT = int(os.environ.get("LONGHORN_BACKUP_KUBECTL_TIMEOUT_S", "30"))

kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, TIMEOUT)


def _bool_env(name: str, default: str = "True") -> bool:
    """Parses a Jinja `{{ var | bool }}` render, which Python's str() spells "True"/"False"."""
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def _hhmm_from_two_field_cron(spec: str) -> str:
    """`MINUTE HOUR ...` -> `H:MM`, matching the shell's `awk '{ print $2 ":" $1 }'`.

    Works for both a full 5-field cron ("30 3 * * *") and the weekly minute-hour pair
    ("30 4") — both put minute first and hour second.
    """
    fields = spec.split()
    minute, hour = fields[0], fields[1]
    return f"{hour}:{minute}"


BACKUP_ARMED = _bool_env("LONGHORN_BACKUP_ARMED")
R2_ARMED = _bool_env("LONGHORN_R2_ARMED")
MAX_AGE_HOURS = int(os.environ.get("LONGHORN_BACKUP_MAX_AGE_HOURS", "30"))
WEEKLY_MAX_AGE_HOURS = int(
    os.environ.get("LONGHORN_WEEKLY_BACKUP_MAX_AGE_HOURS", "198")
)
ERROR_MAX_AGE_HOURS = int(os.environ.get("LONGHORN_BACKUP_ERROR_MAX_AGE_HOURS", "24"))
DAILY_BACKUP_BUDGET = int(os.environ.get("LONGHORN_DAILY_BACKUP_BUDGET", "16"))
DAILY_RUN_HHMM = _hhmm_from_two_field_cron(
    os.environ.get("LONGHORN_BACKUP_CRON", "30 3 * * *")
)
WEEKLY_RUN_HHMM = _hhmm_from_two_field_cron(
    os.environ.get("LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR", "30 4")
)
DRILL_STAMP_DIR = os.environ.get(
    "LONGHORN_RESTORE_DRILL_STAMP_DIR", "/var/lib/longhorn-restore-drill"
)
DRILL_MAX_AGE_DAYS = int(os.environ.get("LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS", "3"))
DRILL_COVERAGE_SLACK_DAYS = int(
    os.environ.get("LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS", "5")
)

MAX_AGE_S = MAX_AGE_HOURS * 3600
WEEKLY_MAX_AGE_S = WEEKLY_MAX_AGE_HOURS * 3600


def _read_stamp(path: str) -> str | None:
    """A stamp file's content with trailing newlines stripped, or None if unreadable.

    Matches bash `$(cat "$path" 2>/dev/null)`: command substitution strips every trailing
    newline, and a missing/unreadable file becomes an empty string there — which the shell then
    tests with `[[ -r ]]` separately. Folded into one None-or-content result here.
    """
    try:
        with open(path) as fh:
            return fh.read().rstrip("\n")
    except OSError:
        return None


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
    success = {}
    for cand in candidates:
        content = _read_stamp(os.path.join(DRILL_STAMP_DIR, "success", cand))
        if content is not None:
            success[cand] = content
    return success


def main() -> int:
    now_s = time.time()

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
            DAILY_RUN_HHMM,
            "*",
        ),
        *[
            (
                f"recurring-job-group.longhorn.io/weekly-backup-d{shard}=enabled",
                WEEKLY_MAX_AGE_S,
                f"weekly-d{shard}",
                f"weekly-backup-d{shard}",
                WEEKLY_RUN_HHMM,
                str(shard),
            )
            for shard in range(7)
        ],
        (
            "recurring-job-group.longhorn.io/weekly-backup=enabled",
            WEEKLY_MAX_AGE_S,
            "weekly-legacy",
            "__no_job__",
            WEEKLY_RUN_HHMM,
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
    drill_content = _read_stamp(drill_stamp_path)
    drill_max_age_s = DRILL_MAX_AGE_DAYS * 86400
    drill_problem = logic.check_restore_drill(
        drill_content, drill_stamp_path, now_s, drill_max_age_s, DRILL_MAX_AGE_DAYS
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
    import subprocess

    subprocess.run(
        ["logger", "-t", "longhorn-backup-health", f"status={status} {msg}"],
        check=False,
    )

    print("%s\t%s" % (status, push_msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
