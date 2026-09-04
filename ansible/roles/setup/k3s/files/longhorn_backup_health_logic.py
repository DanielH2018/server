"""Pure decision core for the Longhorn backup-plane heartbeat (longhorn_backup_health.py).

Ported verbatim from longhorn-backup-health.sh.j2's eight `add()` checks. Split from the I/O
shell (host-side kubectl reads, stamp-file reads) the same way configarr_health_logic.py is: this
stays stdlib-only, takes every clock reading and file read as an argument, and is unit-testable
without a cluster. `now_s` is always injected rather than read from `time.time()` here, so a test
can pin it.

Every function below corresponds to one numbered check in the original script's comments, and the
message text is copied character-for-character — several are matched by downstream tests/greps
and by an operator's muscle memory reading `journalctl -t longhorn-backup-health`.
"""

from __future__ import annotations

import datetime as _dt
import re

# 6h slack after a volume's first scheduled run before it is called uncovered. Hardcoded in the
# original script too (`GRACE_SLACK_S=$(( 6 * 3600 ))`), not templated.
GRACE_SLACK_S = 6 * 3600

_STAMP_RE = re.compile(r"[0-9]+")
_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"
_FRACTIONAL_SECONDS_RE = re.compile(r"\.\d+")


def rfc3339_to_epoch(ts: str) -> float | None:
    """Seconds since the epoch for an RFC3339 timestamp, or None if unparseable.

    Mirrors `date -d "$ts" +%s 2>/dev/null` returning empty on failure. `.metadata
    .creationTimestamp` is a Kubernetes metav1.Time and always seconds-precision UTC with a
    trailing Z, but `.status.snapshotCreatedAt` is a plain string Longhorn writes itself and
    carries no such guarantee — `date -d` accepts fractional seconds and a numeric UTC offset,
    so this must too, or a sub-second stamp reads as unparseable and pages a false RED on every
    tick. Fractional digits are dropped (bash's `date -d` truncates them too, and the checks here
    only ever compare at whole-second resolution) before falling back to the strict seconds-only
    path for plain `...Z` input.
    """
    if not ts:
        return None
    normalized = _FRACTIONAL_SECONDS_RE.sub("", ts)
    try:
        return _dt.datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return (
            _dt.datetime.strptime(normalized, _RFC3339)
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def first_run_after(from_s: float, hhmm: str, dow: str) -> int:
    """Epoch of the first scheduled run strictly after `from_s`.

    The job fires at `hhmm` (H:MM, LOCAL time — matching the shell's unqualified `date`) on
    weekday `dow` (0-6, 0=Sunday) or every day (`dow == "*"`). Returns 0 if it cannot be
    computed — mirrors first_run_after() in longhorn-backup-health.sh.j2 exactly, including its
    8-day search bound.
    """
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
    except ValueError, AttributeError:
        return 0
    for day in range(8):
        try:
            probe_dt = _dt.datetime.fromtimestamp(from_s + day * 86400)  # noqa: DTZ006 (local time, deliberately — see docstring)
            probe_dt = probe_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError, OverflowError, OSError:
            continue
        probe_s = int(probe_dt.timestamp())
        if probe_s <= from_s:
            continue
        # Python's isoweekday(): Monday=1 .. Sunday=7. `% 7` maps Sunday to 0, matching `date +%w`.
        if dow == "*" or str(probe_dt.isoweekday() % 7) == dow:
            return probe_s
    return 0


# ── check 1: is the backup store reachable? ──────────────────────────────────────────────────


def check_target_availability(targets: dict[str, dict]) -> list[tuple[int, str]]:
    """Whether each armed backup target reports available.

    `targets` is {name: {"raw": <status.available string, incl. any error text>, "reason":
    <status.conditions[0].message, truncated, empty on read failure>}}, built by the I/O layer
    in BACKUP_TARGETS order (armed targets only — a disarmed target is never queried).
    """
    problems = []
    for name, info in targets.items():
        raw = info.get("raw", "")
        if raw != "true":
            reason = info.get("reason") or ""
            problems.append((1, f"backup target {name} unavailable: {reason or raw}"))
    return problems


# ── check 2: has anything landed recently? ───────────────────────────────────────────────────


def check_freshness(
    newest_ts: str | None, now_s: float, max_age_s: float, max_age_hours: int
) -> tuple[tuple[int, str] | None, float]:
    """Whether the newest backup is fresh enough, as (problem_or_None, age_s).

    `newest_ts` is the lexicographically-newest snapshotCreatedAt across every Backup (mirrors
    `sort -r | head -1` on RFC3339 strings).
    """
    if not newest_ts:
        return (1, "no backups exist"), 0
    newest_s = rfc3339_to_epoch(newest_ts)
    if newest_s is None:
        return (1, f"unparseable backup timestamp: {newest_ts}"), 0
    age_s = now_s - newest_s
    if age_s > max_age_s:
        return (
            1,
            f"newest backup is {int(age_s // 3600)}h old (limit {max_age_hours}h)",
        ), age_s
    return None, age_s


# ── check 3: did a backup reach Error state? ─────────────────────────────────────────────────


def check_errored_backups(
    backup_items: list[dict], cutoff_s: float, error_max_age_hours: int
) -> tuple[int, str] | None:
    """`backup_items` is `.items` from `kubectl get backups.longhorn.io -o json`."""
    names = []
    for item in backup_items:
        status = item.get("status") or {}
        if status.get("state") != "Error":
            continue
        created_s = rfc3339_to_epoch(
            (item.get("metadata") or {}).get("creationTimestamp") or ""
        )
        if created_s is None or not (created_s > cutoff_s):
            continue
        meta = item.get("metadata") or {}
        vol = (meta.get("labels") or {}).get("backup-volume") or "volume unknown"
        names.append(f"{meta.get('name')} ({vol})")
    if not names:
        return None
    return 2, f"backups that failed in the last {error_max_age_hours}h: " + ", ".join(
        names
    )


# ── check 4: is every backed-up volume covered? ──────────────────────────────────────────────


class TierResult:
    """Accumulator matching the global bash vars check_tier() mutates across every call."""

    def __init__(self) -> None:
        self.uncovered: list[str] = []
        self.checked = 0
        self.suppressed = 0
        self.graced = 0
        self.graced_vols: list[str] = []
        self.graced_new = 0
        self.graced_new_vols: list[str] = []
        self.graced_seeded = 0
        self.graced_seeded_vols: list[str] = []


def check_tier(
    result: TierResult,
    rows: list[tuple[str, str, str, str]],
    coverage_rows: list[tuple[str, str, str]],
    max_age_s: float,
    tier: str,
    job: str,
    run_hhmm: str,
    dow: str,
    disarmed_targets: set[str],
    now_s: float,
) -> None:
    """Ports check_tier() from the shell, mutating `result` in place.

    check_tier() is called once per backup tier (daily, 7 weekly shards, weekly-legacy) and
    every call shares the same global counters in the original script, which is what makes
    UNCOVERED/CHECKED/etc. fleet-wide totals rather than per-tier ones.

    `rows` is (volume, creationTimestamp, namespace/pvcName, backupTargetName) for the volumes
    this tier's label selector matches. `coverage_rows` is (volumeName, snapshotCreatedAt,
    RecurringJob) across every Backup, shared unfiltered across every tier.
    """
    for vol, created, claim, target in rows:
        if not vol:
            continue
        target = target or "default"
        if target in disarmed_targets:
            result.suppressed += 1
            continue

        latest_candidates = [
            ts for (v, ts, j) in coverage_rows if v == vol and ts and j == job
        ]
        latest = max(latest_candidates) if latest_candidates else None

        if latest is None:
            created_s = rfc3339_to_epoch(created) or 0
            first_run_s = (
                first_run_after(created_s, run_hhmm, dow) if created_s > 0 else 0
            )
            if (
                created_s > 0
                and first_run_s > 0
                and now_s < first_run_s + GRACE_SLACK_S
            ):
                result.graced += 1
                result.graced_vols.append(claim)
                any_candidates = [ts for (v, ts, j) in coverage_rows if v == vol and ts]
                if any_candidates:
                    result.graced_seeded += 1
                    result.graced_seeded_vols.append(claim)
                else:
                    result.graced_new += 1
                    result.graced_new_vols.append(claim)
                continue
            if job == "__no_job__":
                result.uncovered.append(
                    f"{claim} ({tier}, no recurring job selects it)"
                )
            else:
                result.uncovered.append(f"{claim} ({tier}, no backup from {job})")
            continue

        result.checked += 1
        latest_s = rfc3339_to_epoch(latest) or 0
        if latest_s == 0:
            result.uncovered.append(f"{claim} ({tier}, unparseable timestamp)")
        elif now_s - latest_s > max_age_s:
            hours = int((now_s - latest_s) // 3600)
            result.uncovered.append(f"{claim} ({tier}, {hours}h)")


def uncovered_problem(result: TierResult) -> tuple[int, str] | None:
    if not result.uncovered:
        return None
    return 3, "backed-up volumes stale or missing: " + ", ".join(result.uncovered)


# ── check 5: is the daily backup count outgrowing B2's transaction budget? ──────────────────


def compute_recent_backups(
    rows: list[tuple[str, str, str]], r2_volumes: set[str], day_ago_s: float
) -> tuple[int, int]:
    """Returns (RECENT_N, RECENT_BYTES) for backups created in the last 24h.

    `rows` is (volumeName, snapshotCreatedAt, size) across every Backup, counting only volumes
    NOT routed to R2 (see the script's own comment on why: R2 spends Cloudflare's allowance,
    not B2's).
    """
    n = 0
    total = 0
    for vol, created, size in rows:
        if not created:
            continue
        if vol in r2_volumes:
            continue
        created_s = rfc3339_to_epoch(created)
        if created_s is None or created_s < day_ago_s:
            continue
        n += 1
        # Mirrors `$(( RECENT_BYTES + ${SIZE:-0} ))`: bash arithmetic treats a non-numeric SIZE
        # as 0 rather than raising, and `.status.size` is a plain string Longhorn writes with no
        # format guarantee — int() must fail the same way it fails open, not crash the reader.
        try:
            total += int(size) if size else 0
        except ValueError:
            pass
    return n, total


def check_recent_budget(
    recent_n: int, recent_bytes: int, budget: int
) -> tuple[int, str] | None:
    if recent_n <= budget:
        return None
    mib = recent_bytes // 1048576
    return (
        4,
        f"{recent_n} backups in 24h exceeds the {budget} budget ({mib} MiB) — Longhorn's share "
        "of B2's transaction cap; cut backup scope or raise the cap before it 403s",
    )


# ── check 6: did the JOB fail, rather than the backup? ───────────────────────────────────────


def check_failed_jobs(
    job_items: list[dict], cutoff_s: float, error_max_age_hours: int
) -> tuple[int, str] | None:
    """`job_items` is `.items` from `kubectl get jobs.batch -o json`."""
    names = []
    for item in job_items:
        status = item.get("status") or {}
        failed = [
            c
            for c in (status.get("conditions") or [])
            if c.get("type") == "Failed" and c.get("status") == "True"
        ]
        if not failed:
            continue
        at = (
            failed[0].get("lastTransitionTime")
            or (item.get("metadata") or {}).get("creationTimestamp")
            or ""
        )
        at_s = rfc3339_to_epoch(at)
        if at_s is None or not (at_s > cutoff_s):
            continue
        names.append((item.get("metadata") or {}).get("name") or "")
    if not names:
        return None
    return (
        2,
        f"backup job(s) that failed in the last {error_max_age_hours}h: "
        + ", ".join(names),
    )


# ── check 7: do the backups actually restore? ────────────────────────────────────────────────


def check_restore_drill(
    stamp_content: str | None,
    stamp_path: str,
    now_s: float,
    max_age_s: float,
    max_age_days: int,
) -> tuple[int, str] | None:
    """Whether the restore drill's last success is fresh enough.

    `stamp_content` is the stamp file's content with any trailing newline already stripped
    (matching bash `$(cat ...)` command substitution), or None if the file is missing/unreadable.
    """
    if stamp_content is None:
        return (
            3,
            f"no restore drill has ever succeeded (no {stamp_path}) — backups are unproven",
        )
    if not _STAMP_RE.fullmatch(stamp_content):
        return (
            3,
            "restore-drill stamp is unreadable — treating the restore path as unproven",
        )
    drill_at = int(stamp_content)
    age_s = now_s - drill_at
    if age_s > max_age_s:
        return (
            3,
            f"last successful restore drill was {int(age_s // 86400)}d ago "
            f"(limit {max_age_days}d)",
        )
    return None


# ── check 8: is every volume covered by the restore-drill rotation? ─────────────────────────


def check_restore_coverage(
    candidates: list[str],
    seen: dict[str, float],
    success: dict[str, str],
    now_s: float,
    coverage_slack_days: int,
) -> tuple[int, str] | None:
    """Which restore-drill candidates have gone too long unproven.

    `candidates` is the drill's published eligible-volume list (one per line, order preserved).
    `seen`/`success` map a candidate to its join-marker mtime / success-stamp content (already
    newline-stripped), present only when that file exists and was readable.
    """
    cand_n = len(candidates)
    if cand_n == 0:
        return None
    coverage_max_s = (cand_n + coverage_slack_days) * 86400

    unproven = []
    for cand in candidates:
        seen_at = seen.get(cand)
        if seen_at is None:
            continue
        if not (now_s - seen_at > coverage_max_s):
            continue
        cand_at_raw = success.get(cand)
        ok = False
        if cand_at_raw is not None and _STAMP_RE.fullmatch(cand_at_raw):
            ok = now_s - int(cand_at_raw) <= coverage_max_s
        if not ok:
            unproven.append(cand)

    if not unproven:
        return None
    return (
        3,
        f"volume(s) not restore-proven in {int(coverage_max_s // 86400)}d: "
        + ", ".join(unproven),
    )


# ── final verdict assembly ───────────────────────────────────────────────────────────────────


def build_verdict(
    problems: list[tuple[int, str]],
    *,
    backup_targets: list[str],
    disarmed_targets: list[str],
    age_s: float,
    checked: int,
    recent_n: int,
    daily_backup_budget: int,
    suppressed: int,
    graced_new: int,
    graced_new_vols: list[str],
    graced_seeded: int,
    graced_seeded_vols: list[str],
) -> tuple[str, str, str]:
    """Returns (status, msg, push_msg).

    `msg` is the full unranked, semicolon-joined problem list (or the green summary) — what the
    syslog line carries. `push_msg` is what actually goes to Kuma/Healthchecks: on a DOWN
    verdict, the single highest-severity problem plus a count of the rest, so triage isn't glued
    to a long tail (see add()'s rank scale in the shell script).
    """
    if not problems:
        disarmed_note = ""
        if disarmed_targets:
            disarmed_note = f" — DISARMED: {' '.join(disarmed_targets)} ({suppressed} volume(s) not checked)"
        graced_note = ""
        if graced_new:
            graced_note += (
                f" — {graced_new} volume(s) awaiting their first scheduled backup, "
                f"no offsite copy at all: {', '.join(graced_new_vols)}"
            )
        if graced_seeded:
            graced_note += (
                f" — {graced_seeded} volume(s) seeded but not yet in rotation, "
                f"first scheduled run pending: {', '.join(graced_seeded_vols)}"
            )
        targets_str = " ".join(backup_targets) if backup_targets else "none"
        msg = (
            f"backup target(s) {targets_str} available, newest backup {int(age_s // 3600)}h old, "
            f"{checked} backed-up volume(s) covered across daily+weekly, {recent_n} B2 backup(s)"
            f"/24h (budget {daily_backup_budget}){disarmed_note}{graced_note}"
        )
        return "up", msg, msg

    msg = "; ".join(text for _, text in problems)
    ranked = sorted(problems, key=lambda p: p[0])
    top = ranked[0][1]
    rest = len(problems) - 1
    push_msg = (
        f"{top} (+{rest} more: see journalctl -t longhorn-backup-health)"
        if rest > 0
        else top
    )
    return "down", msg, push_msg
