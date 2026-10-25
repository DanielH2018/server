#!/usr/bin/env python3
"""Longhorn backup-plane heartbeat — the cluster-reading half of longhorn-backup-health.sh.

Reads the cluster through the read-only ServiceAccount kubeconfig and the restore drill's stamps,
from the drill's ConfigMap or, while that holds no `last-success`, from the files on disk. Then prints one tab-separated line, `up<TAB>msg` or `down<TAB>msg`, for the
wrapper to log and push to Kuma. It never exits nonzero on a bad verdict: a DOWN is data to
report, not a crash. Same shape as configarr_health.py: this file does the I/O,
longhorn_backup_health_logic.py holds every threshold and decides up-or-down, and the split is
what makes the decisions unit-testable without a cluster.

Runs on daniel-box via `uv run --no-project --python <pin>` (host_python_version in
ansible/inventory/group_vars/all.yml). The Kuma push token stays in the templated wrapper, which
sources it from an 0640 env file at runtime, and never appears here.

Usage: longhorn_backup_health.py   (takes no arguments; -h/--help prints this text)

Not run by hand: /usr/local/bin/longhorn-backup-health (rendered from
templates/longhorn-backup-health.sh.j2, a cron every 10 minutes) exports every setting and
calls it. It refuses to start unless the shim exported all of them: LONGHORN_BACKUP_NAMESPACE,
LONGHORN_BACKUP_KUBECTL, LONGHORN_BACKUP_KUBECTL_TIMEOUT_S, the LONGHORN_*_MAX_AGE_HOURS and
LONGHORN_DAILY_BACKUP_BUDGET thresholds, LONGHORN_RESTORE_DRILL_* (stamp dir, namespace and
ages), LONGHORN_TRIM_CRON_* and LONGHORN_B2_DELETIONS_CRON_* (cron file, expected flag, fired
stamp), LONGHORN_JOURNALCTL, LONGHORN_JOURNAL_TIMEOUT_S and LONGHORN_CRON_EVIDENCE_WINDOW_HOURS.

It takes no arm flag and no schedule from the shim. Each backup target's arm state comes from the
cluster: a blank `spec.backupTargetURL` is a disarmed target (H1.7a). The daily and weekly run
times, which size the first-run grace, come from the `spec.cron` of the live RecurringJobs. Both
are one list call each, so the reader reports what the cluster holds, whichever tool wrote it:
Flux, through the `longhorn-backup` tag, or an operator's break-glass. No baked copy of a flag or
a schedule can disagree with it. A target that carries the reconcile-disabled annotation is a
problem the reader reports, never a disarm (H1.7b).
Prints `up<TAB>msg` or `down<TAB>msg` and exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_lib
from longhorn_lib import longhorn_backup_health_logic as logic
from longhorn_lib import longhorn_backups as backups
from longhorn_lib import longhorn_cluster_state as cluster_state
from longhorn_lib import longhorn_cron_evidence_logic as cron_evidence
from longhorn_lib import longhorn_restore_drill_stamps as drill_stamps
from longhorn_lib import longhorn_skipped_volumes_logic as skipped


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
    """Parses a Jinja `{{ var | bool }}` render, which Python's str() spells "True"/"False".

    Refuses anything it doesn't recognize rather than defaulting to False. The two
    LONGHORN_*_CRON_EXPECTED flags route through this, and False means a cron is not expected,
    so its absence goes unreported. A typo'd or truncated export (a template change that renders
    `Tru` instead of `True`, say) used to fold silently into False here, the same wrong-default
    direction `_require_env` above exists to close for a missing var entirely.
    """
    raw = _require_env(name).strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise SystemExit(f"env var {name}={raw!r} is not a recognized boolean")


NAMESPACE = _require_env("LONGHORN_BACKUP_NAMESPACE")
KUBECTL_BIN = _require_env("LONGHORN_BACKUP_KUBECTL")
TIMEOUT = _require_int_env("LONGHORN_BACKUP_KUBECTL_TIMEOUT_S")

kubectl = host_lib.kubectl_runner(KUBECTL_BIN, NAMESPACE, TIMEOUT)

MAX_AGE_HOURS = _require_int_env("LONGHORN_BACKUP_MAX_AGE_HOURS")
WEEKLY_MAX_AGE_HOURS = _require_int_env("LONGHORN_WEEKLY_BACKUP_MAX_AGE_HOURS")
ERROR_MAX_AGE_HOURS = _require_int_env("LONGHORN_BACKUP_ERROR_MAX_AGE_HOURS")
DAILY_BACKUP_BUDGET = _require_int_env("LONGHORN_DAILY_BACKUP_BUDGET")
DRILL_STAMP_DIR = _require_env("LONGHORN_RESTORE_DRILL_STAMP_DIR")
# The drill's stamp ConfigMap lives in the drill's namespace, not Longhorn's, so it gets its own
# runner rather than a second `-n` passed through the longhorn-system one.
DRILL_NAMESPACE = _require_env("LONGHORN_RESTORE_DRILL_NAMESPACE")
drill_kubectl = host_lib.kubectl_runner(KUBECTL_BIN, DRILL_NAMESPACE, TIMEOUT)
DRILL_MAX_AGE_DAYS = _require_int_env("LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS")
DRILL_COVERAGE_SLACK_DAYS = _require_int_env(
    "LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS"
)
CRON_EVIDENCE_WINDOW_HOURS = _require_int_env("LONGHORN_CRON_EVIDENCE_WINDOW_HOURS")
# Check 10's two crons. The path, the fire stamp and the expected flag are shim-exported for the reason
# the transport settings are: a value only the shim knows must not sit here as a default a
# wrong export would shadow. The flag differs per cron — the B2-accounting task is gated on
# `has_repo_checkout`, the trim task is not.
TRIM_CRON = cron_evidence.cron_state(
    "longhorn-trim",
    _require_env("LONGHORN_TRIM_CRON_FILE"),
    _require_bool_env("LONGHORN_TRIM_CRON_EXPECTED"),
    _require_env("LONGHORN_TRIM_CRON_FIRED_STAMP"),
)
B2_DELETIONS_CRON = cron_evidence.cron_state(
    "b2-deletions",
    _require_env("LONGHORN_B2_DELETIONS_CRON_FILE"),
    _require_bool_env("LONGHORN_B2_DELETIONS_CRON_EXPECTED"),
    _require_env("LONGHORN_B2_DELETIONS_CRON_FIRED_STAMP"),
)

# Check 9's transport. Its own deadline rather than TIMEOUT: journalctl answers from a local
# file in milliseconds, and the shim sizes the reader's worst-case run against its 600s cron
# period, where two more 30s calls would not fit.
journal = host_lib.journal_reader(
    _require_env("LONGHORN_JOURNALCTL"),
    CRON_EVIDENCE_WINDOW_HOURS,
    _require_int_env("LONGHORN_JOURNAL_TIMEOUT_S"),
)

MAX_AGE_S = MAX_AGE_HOURS * 3600
WEEKLY_MAX_AGE_S = WEEKLY_MAX_AGE_HOURS * 3600


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


# DECIDED: a kubectl fetch that fails is a DOWN with a reason, not an empty result. The bash
# this was ported from sent every one of these reads to `2>/dev/null`
# (`git show 62c488dd^:ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2`, lines
# 144, 176, 218, 344, 395, 412 and 443) and let the empty capture flow into the checks; the first
# Python port kept that shape as `... if rc == 0 else []`. Both read an API-server timeout, a 500
# or RBAC drift as "nothing errored, nothing is stale, this tier has no volumes" — a green light
# over data nobody looked at, on the deadman for the Longhorn backup plane. Parity with the bash
# was the wrong target here, so this departs from it: a reader that cannot see the cluster says
# so. Issue #1061 has the per-line analysis.
#
# The one thing that is NOT a failure is rc == 0 with empty output: that is the ordinary answer
# for eight of the nine tier selectors and for the r2 volume set, and flagging it would
# false-DOWN a healthy cluster on every tick.
def _fetch_text(problems: list[tuple[int, str]], what: str, *args: str) -> str | None:
    """Output of one kubectl fetch, or None having appended a problem naming `what` and the rc.

    The appended problems join the same list logic.build_verdict() ranks. Rank 1 (the scale in
    build_verdict's docstring) puts a fetch failure behind "backup target unavailable" for the
    push slot — sorted() is stable and check 1 appends first — and ahead of everything else.
    """
    rc, out = kubectl(*args)
    if rc != 0:
        detail = out.strip()[:160] or "(no output)"
        problems.append((1, f"{what} fetch failed (rc={rc}): {detail}"))
        return None
    return out


def _fetch_items(
    problems: list[tuple[int, str]], what: str, *args: str
) -> list[dict] | None:
    """`.items` from a kubectl `-o json` fetch, or None having appended a problem.

    The body is checked structurally rather than by catching ValueError alone: `json.loads`
    accepts the literal `null` and returns None, so a truncated or empty-but-successful read
    used to reach `.get("items")` as an AttributeError instead of a verdict.
    """
    out = _fetch_text(problems, what, *args)
    if out is None:
        return None
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        detail = out.strip()[:160] or "(empty)"
        problems.append((1, f"{what} fetch returned an unparseable body: {detail}"))
        return None
    return items


def _fetch_targets(problems: list[tuple[int, str]]) -> dict[str, dict] | None:
    """Every BackupTarget by name, from one list call, or None having appended a problem.

    DECIDED: a failed list, or a list that lacks a target, is a rank-1 problem and never a
    disarm. A blank URL is the one deliberate disarm, and it can only be read off an object that
    came back. This departs from `lib.longhorn_cluster.backup_target_url`, which returns "" for
    a failed read as well: that reader declines to classify, and this one is the deadman for
    the backup plane (failure class 3). ADR-0007, *Consequences*, has the monitor's side.
    """
    items = _fetch_items(
        problems, "backup target list", "get", "backuptargets.longhorn.io", "-o", "json"
    )
    return None if items is None else cluster_state.targets_by_name(items)


def _fetch_job_crons(problems: list[tuple[int, str]]) -> dict[str, str] | None:
    """Every RecurringJob's `spec.cron` by name from one list call, or None with a problem."""
    items = _fetch_items(
        problems, "recurring job list", "get", "recurringjobs.longhorn.io", "-o", "json"
    )
    return None if items is None else cluster_state.job_crons_by_name(items)


def _fetch_backups_json(problems: list[tuple[int, str]]) -> list[dict] | None:
    """The backup list, fetched once a run; checks 2 to 5 all read it (#3735)."""
    return _fetch_items(problems, "backup list", *backups.LIST_ARGS)


def _fetch_jobs_json(problems: list[tuple[int, str]]) -> list[dict] | None:
    return _fetch_items(problems, "failed-jobs", "get", "jobs.batch", "-o", "json")


def _fetch_r2_volumes(problems: list[tuple[int, str]]) -> set[str] | None:
    out = _fetch_text(
        problems,
        "r2 volume set",
        "get",
        "volumes.longhorn.io",
        "-o",
        'jsonpath={range .items[?(@.spec.backupTargetName=="%s")]}{.metadata.name}'
        '{" "}{end}' % backups.R2_TARGET,
    )
    if out is None:
        return None
    return set(out.split())


def _fetch_drill_stamps(
    problems: list[tuple[int, str]],
) -> drill_stamps.DrillStamps | None:
    """The store checks 7 and 8 read this run, or None having appended a problem.

    An absent ConfigMap, or one with no `last-success`, falls back to the host directory: that is
    the live state until the drill writes the ConfigMap (H2.3c). A failed read falls back to
    nothing. It is a rank-1 problem like every other failed fetch here, and checks 7 and 8 are
    skipped, because a directory the drill may have stopped writing is not evidence once the
    ConfigMap could hold the current state.
    """
    rc, out = drill_kubectl(*drill_stamps.CONFIGMAP_GET_ARGS)
    if rc != 0:
        detail = out.strip()[:160] or "(no output)"
        problems.append(
            (1, f"restore-drill stamp ConfigMap fetch failed (rc={rc}): {detail}")
        )
        return None
    try:
        doc = drill_stamps.parse_configmap(out)
    except ValueError:
        detail = out.strip()[:160] or "(empty)"
        problems.append(
            (
                1,
                f"restore-drill stamp ConfigMap fetch returned an unparseable body: {detail}",
            )
        )
        return None
    return drill_stamps.select_stamps(doc, DRILL_NAMESPACE, DRILL_STAMP_DIR)


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


def _check_restore_drill(
    problems: list[tuple[int, str]], stamps: drill_stamps.DrillStamps, now_s: float
) -> None:
    """Check 7 (freshness), check 8 (rotation coverage) and the oversize and no-PVC checks."""
    drill_problem = logic.check_restore_drill(
        stamps.last_success,
        stamps.where,
        now_s,
        DRILL_MAX_AGE_DAYS * 86400,
        DRILL_MAX_AGE_DAYS,
        stamp_unreadable=stamps.last_success_unreadable,
    )
    coverage_problem = logic.check_restore_coverage(
        stamps.candidates,
        stamps.seen,
        stamps.success,
        now_s,
        DRILL_COVERAGE_SLACK_DAYS,
        seen_default=stamps.seen_default,
    )
    oversize_problem = logic.check_restore_drill_oversize(stamps.excluded_oversize)
    nopvc_problem = logic.check_restore_drill_nopvc(stamps.excluded_nopvc)
    for problem in (drill_problem, coverage_problem, oversize_problem, nopvc_problem):
        if problem:
            problems.append(problem)


def main(now: float | None = None) -> int:
    """Run the checks and print the up/down<TAB>msg verdict.

    `now` is the epoch every age is measured from; the cron leaves it None and reads the
    clock. A test hands one in so its fixtures date against a fixed epoch rather than the
    suite's own runtime (#2220) — through this argument, not an env var, so nothing in the
    production environment can shift what the script thinks the time is.
    """
    now_s = time.time() if now is None else now

    problems: list[tuple[int, str]] = []

    # ── targets (armed only) and disarmed set ─────────────────────────────────────────────
    # The arm state is the live URL: blank is disarmed (`cluster_state.watch_plan` has the rule
    # and why a target the list lacks stays watched).
    targets = _fetch_targets(problems)
    backup_targets, disarmed_targets, plan_problems = cluster_state.watch_plan(targets)
    problems += plan_problems
    listed = targets or {}
    problems += logic.check_target_availability(
        {name: listed[name] for name in backup_targets if name in listed}
    )

    # ── run times, for the first-run grace ────────────────────────────────────────────────
    # From each tier's live RecurringJob. One whose cron is missing or malformed degrades the
    # first-run grace for that tier, not the reader (the 2026-09-04 review's finding #4).
    job_crons = _fetch_job_crons(problems)
    run_hhmm = cluster_state.run_times(job_crons or {})
    for job, hhmm in run_hhmm.items():
        if hhmm is None and job_crons:
            _syslog(
                f"RecurringJob {job} has cron {job_crons.get(job)!r}, which has no minute "
                "and hour; disabling that tier's first-run grace"
            )
    daily_run_hhmm = run_hhmm[backups.DAILY_JOB]
    legacy_run_hhmm = cluster_state.legacy_run_time(run_hhmm)

    # One fetch feeds checks 2 to 5. On a failed fetch each of them is skipped rather than fed
    # an empty list, so the one fetch problem is the one thing reported.
    backup_items = _fetch_backups_json(problems)
    backup_list = backups.from_items(backup_items) if backup_items is not None else None

    # ── check 2: freshness ────────────────────────────────────────────────────────────────
    # On a failed fetch, check_freshness is skipped rather than fed a None newest_ts: that path
    # reports "no backups exist", a claim about the data plane this reader has not seen. `age_s`
    # stays 0 and is never read — the fetch problem already forces build_verdict's DOWN branch,
    # and only the green summary prints an age.
    age_s = 0.0
    if backup_list is not None:
        newest = backups.newest(backup_list)
        fresh_problem, age_s = logic.check_freshness(
            newest.created if newest else None, now_s, MAX_AGE_S, MAX_AGE_HOURS
        )
        if fresh_problem:
            problems.append(fresh_problem)

    # ── check 3: errored backups ──────────────────────────────────────────────────────────
    error_cutoff_s = now_s - ERROR_MAX_AGE_HOURS * 3600
    if backup_items is not None:
        errored_problem = logic.check_errored_backups(
            backup_items, error_cutoff_s, ERROR_MAX_AGE_HOURS
        )
        if errored_problem:
            problems.append(errored_problem)

    # ── check 4: per-tier coverage ────────────────────────────────────────────────────────
    disarmed_set = set(disarmed_targets)
    result = logic.TierResult()
    tiers = [
        (
            backups.group_label(backups.DEFAULT_GROUP) + "=enabled",
            MAX_AGE_S,
            "daily",
            backups.DAILY_JOB,
            daily_run_hhmm,
            "*",
        ),
        *[
            (
                backups.group_label(backups.weekly_shard_group(shard)) + "=enabled",
                WEEKLY_MAX_AGE_S,
                f"weekly-d{shard}",
                backups.weekly_shard_group(shard),
                run_hhmm[backups.weekly_shard_group(shard)],
                str(shard),
            )
            for shard in range(backups.WEEKLY_SHARDS)
        ],
        (
            backups.group_label(backups.WEEKLY_LEGACY_GROUP) + "=enabled",
            WEEKLY_MAX_AGE_S,
            "weekly-legacy",
            "__no_job__",
            legacy_run_hhmm,
            "*",
        ),
    ]
    # The whole loop is skipped when the backup list fetch failed: every tier is matched
    # against those rows, so feeding it an empty list would report all nine tiers' volumes as
    # uncovered over one unread fetch. The fetch's own problem is already in `problems`.
    coverage_rows = [(b.volume, b.created, b.job) for b in backup_list or []]
    # Check 11's scope: the weekly-shard volumes on an armed target, as volume -> claim. The
    # daily tier is left out on purpose: `DECIDED: only weekly-tier volumes are checked` in
    # longhorn_skipped_volumes_logic.py.
    weekly_watched: dict[str, str] = {}
    for selector, max_age_s, tier, job, run_hhmm, dow in (
        tiers if backup_list is not None else []
    ):
        rows_raw = _fetch_text(
            problems,
            f"{tier} tier volumes",
            "get",
            "volumes.longhorn.io",
            "-l",
            selector,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{" "}{.metadata.creationTimestamp}'
            '{" "}{.status.kubernetesStatus.namespace}/{.status.kubernetesStatus.pvcName}'
            '{" "}{.spec.backupTargetName}{"\\n"}{end}',
        )
        if rows_raw is None:
            continue
        rows = _parse_volume_rows(rows_raw)
        if job.startswith(backups.WEEKLY_SHARD_PREFIX):
            for vol, _created, claim, target in rows:
                if (target or backups.B2_TARGET) not in disarmed_set:
                    weekly_watched[vol] = claim
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
    r2_volumes = _fetch_r2_volumes(problems)
    # recent_n reaches build_verdict, which prints it only on the green summary — a failed fetch
    # here has already put its own problem in the list, so the 0 is never reported as a count.
    recent_n = 0
    if backup_list is not None and r2_volumes is not None:
        recent_n, recent_bytes = logic.compute_recent_backups(
            [(b.volume, b.created, b.size) for b in backup_list], r2_volumes, day_ago_s
        )
        budget_problem = logic.check_recent_budget(
            recent_n, recent_bytes, DAILY_BACKUP_BUDGET
        )
        if budget_problem:
            problems.append(budget_problem)

    # ── check 6: failed jobs ──────────────────────────────────────────────────────────────
    job_items = _fetch_jobs_json(problems)
    if job_items is not None:
        failed_jobs_problem = logic.check_failed_jobs(
            job_items, error_cutoff_s, ERROR_MAX_AGE_HOURS
        )
        if failed_jobs_problem:
            problems.append(failed_jobs_problem)

    # ── checks 7 and 8: the restore drill, from one store ──────────────────────────────────
    stamps = _fetch_drill_stamps(problems)
    if stamps is not None:
        _check_restore_drill(problems, stamps, now_s)

    # ── checks 9 and 10: what the trim and B2-accounting crons said, and whether they ran ──
    problems.extend(
        cron_evidence.check(
            journal, CRON_EVIDENCE_WINDOW_HOURS, TRIM_CRON, B2_DELETIONS_CRON, now_s
        )
    )

    # ── check 11: weekly volumes a RecurringJob skipped ──────────────────────────────────
    # Longhorn keeps each RecurringJob's newest pod (history limit 1), so its log holds the last
    # run's skips until the next run replaces it. `--tail=-1` because kubectl's default with a
    # selector is the last 10 lines, and the skips are written at the top of a run.
    # `--ignore-errors` because a pod whose container is still starting answers with an error,
    # and failing the whole read on it would page DOWN at random on a run's first seconds. That
    # narrows `DECIDED: a kubectl fetch that fails is a DOWN with a reason` above for one pod's
    # log only, whose skips go unseen for that tick with check 4 still behind them; a failed
    # call as a whole still exits nonzero and is reported. Skipped with the backup list, like
    # check 4, since `weekly_watched` and the coverage rows come from it.
    if backup_list is not None:
        skip_log = _fetch_text(
            problems,
            "recurring-job pod logs",
            "logs",
            "-l",
            "recurring-job.longhorn.io",
            "--prefix",
            "--tail=-1",
            "--ignore-errors",
        )
        if skip_log is not None:
            skipped_problem = skipped.check_skipped_weekly_volumes(
                skipped.parse_skipped_volumes(skip_log), weekly_watched, coverage_rows
            )
            if skipped_problem:
                problems.append(skipped_problem)

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
