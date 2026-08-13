"""Pure decision logic for the Renovate manual-action notifier (no I/O — unit-tested).

Maps open Renovate PRs to an actionable bucket and decides when to (re)notify, so
the I/O shell (renovate_notify.py) only fetches, persists, and posts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Renovate rewrites its Dependency Dashboard issue on every run (~daily on this repo's
# Monday schedule + at-any-time security/lockfile runs). If the dashboard goes stale or
# vanishes, the Renovate App or renovate.json is broken and dependency updates have
# silently stopped — and because there are then NO PRs, the PR digest reads as a healthy
# "backlog cleared". 8 days = comfortably past the weekly cadence without false-firing.
DASHBOARD_STALE_DAYS = 8
DASHBOARD_TITLE = "Dependency Dashboard"

# check-run conclusions that mean "this will not merge" (besides success/neutral/skipped).
_FAIL_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    url: str
    automerge: bool  # Renovate body says Automerge Enabled
    ci: str  # "success" | "pending" | "failure"
    conflicting: bool
    created_at: str = (
        ""  # GitHub's PR `created_at` (ISO-8601), already in the pulls-list payload
    )


def _find_dashboard_issue(issues: list[dict]) -> dict | None:
    """Return the raw Dependency Dashboard issue dict, or None if absent.

    Fed GitHub's `/issues` payload (which also lists PRs — those carry a `pull_request`
    key and are skipped). Matches the dashboard by title AND a renovate-bot author, so a
    human-created look-alike issue can't be mistaken for it. Shared by find_dashboard
    (staleness) and find_dashboard_problems (Repository Problems section) so both read
    the same issue. Pure — unit-tested without HTTP.
    """
    for it in issues:
        if it.get("pull_request"):
            continue
        login = (it.get("user") or {}).get("login", "")
        if it.get("title") == DASHBOARD_TITLE and login.startswith("renovate"):
            return it
    return None


def find_dashboard(issues: list[dict]) -> str | None:
    """Return the Renovate Dependency Dashboard issue's `updated_at`, or None if absent."""
    issue = _find_dashboard_issue(issues)
    return issue.get("updated_at") if issue else None


REPOSITORY_PROBLEMS_HEADER = "## Repository Problems"


def parse_repository_problems(body: str) -> set[str]:
    """Parse the dashboard body's "## Repository Problems" section into a set of problem
    strings — per-package lookup failures, config warnings, etc. Renovate renders each as
    a backtick-wrapped bullet (` - \\`<problem>\\` `) between the header and the next
    top-level `## ` section. This is the bucket the PR digest can't see: a package whose
    lookup starts failing gets no PR and doesn't touch dashboard staleness either (the
    dashboard still updates fine) — it just silently stops receiving updates forever
    (karakeep's gcr.io image, 2026-08). Absent section -> empty set."""
    if REPOSITORY_PROBLEMS_HEADER not in (body or ""):
        return set()
    section = body.split(REPOSITORY_PROBLEMS_HEADER, 1)[1]
    section = section.split("\n## ", 1)[0]  # stop at the next top-level section
    problems = set()
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("-"):
            problems.add(line.lstrip("- ").strip("`"))
    return problems


def find_dashboard_problems(issues: list[dict]) -> set[str]:
    """Parse the dashboard issue's Repository Problems section (see
    parse_repository_problems). Empty set when the dashboard is absent or has no
    problems section."""
    issue = _find_dashboard_issue(issues)
    if issue is None:
        return set()
    return parse_repository_problems(issue.get("body") or "")


def problems_fingerprint(problems: set[str]) -> str:
    """Dedupe key for the Repository Problems bucket. Sorted so ordering is stable, but the
    problem strings themselves are the key — a persistent problem set re-notifies only on
    change, while a NEW problem (even alongside old ones) changes the string and re-pages."""
    return ",".join(sorted(problems))


PROBLEMS_HEADER = (
    "\U0001f9e8 Renovate — Repository Problems (updates silently stalled):"
)


def render_problems(problems: set[str]) -> str:
    lines = [PROBLEMS_HEADER] + [" • %s" % p for p in sorted(problems)]
    return "\n".join(lines)


def dashboard_stale(
    updated_at: str | None,
    now: datetime | None = None,
    max_age_days: int = DASHBOARD_STALE_DAYS,
) -> bool:
    """True if the dependency dashboard is absent or older than `max_age_days`.

    `updated_at` is the issue's ISO-8601 timestamp (GitHub uses a trailing 'Z'), or None
    when no dashboard issue exists. A stale/absent dashboard is the fail-loud signal that
    Renovate itself stopped — the case the 'Renovate Notifier — Alive' monitor (which
    watches the *notifier*, not Renovate) can't see."""
    if not updated_at:
        return True
    now = now or datetime.now(timezone.utc)
    age_days = (
        now - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    ).total_seconds() / 86400
    return age_days > max_age_days


def parse_automerge(body: str) -> bool:
    """True only if Renovate's body explicitly says Automerge Enabled. Absent/unknown
    -> False, so classify_pr() surfaces it as `manual` (fail toward surfacing)."""
    return "Automerge**: Enabled" in (body or "")


def ci_rollup(check_runs: list[dict], statuses: list[dict]) -> str:
    """Fold the two disjoint GitHub CI sources — Checks API (check_runs) and the legacy
    Commit Status API (statuses) — into one verdict. Failure precedes pending precedes
    success: a failure in EITHER source counts."""
    failure = pending = False
    for c in check_runs:
        if c.get("status") != "completed":
            pending = True
        elif c.get("conclusion") in _FAIL_CONCLUSIONS:
            failure = True
    for s in statuses:
        st = s.get("state")
        if st in ("failure", "error"):
            failure = True
        elif st == "pending":
            pending = True
    if failure:
        return "failure"
    if pending:
        return "pending"
    return "success"


def classify_pr(pr: PR) -> str:
    if not pr.automerge:
        return "manual"
    if pr.ci == "failure" or pr.conflicting:
        return "stuck"
    return "on-track"


def actionable(prs: list[PR]) -> list[tuple[PR, str]]:
    """(pr, bucket) for every PR that needs a human — stuck or manual; on-track dropped."""
    out = []
    for pr in prs:
        bucket = classify_pr(pr)
        if bucket in ("stuck", "manual"):
            out.append((pr, bucket))
    return out


CLEARED_MSG = "✅ Renovate backlog cleared — nothing needs your attention."

_BUCKET_ORDER = ("stuck", "manual")
_BUCKET_HEADER = {
    "stuck": "🔧 Stuck (should auto-merge, can't):",
    "manual": "✋ Awaiting your merge (merging → auto-deploys, health-gated, ≤30 min):",
}


# Days-stuck thresholds for the fingerprint's age dimension. Ascending so the loop below keeps
# overwriting `bucket` with the largest one crossed.
_STUCK_AGE_THRESHOLDS = (1, 3, 7, 14)


def _stuck_age_bucket(pr: PR, now: datetime) -> int:
    """Largest `_STUCK_AGE_THRESHOLDS` value the PR's age has crossed, or 0 if under a day old
    or `created_at` is missing/unparseable (age unknown -> no age dimension, same as before)."""
    if not pr.created_at:
        return 0
    try:
        created = datetime.fromisoformat(pr.created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    age_days = (now - created).total_seconds() / 86400
    bucket = 0
    for threshold in _STUCK_AGE_THRESHOLDS:
        if age_days >= threshold:
            bucket = threshold
    return bucket


def fingerprint(items: list[tuple[PR, str]], now: datetime | None = None) -> str:
    """Dedupe key for the actionable PR set. `stuck` PRs carry a coarse age dimension
    (`_stuck_age_bucket`) so a PR that's been stuck for a while re-pages at each threshold
    crossing instead of paging once on day 1 and then going silent forever while it ages
    (PR #67, stuck since 2026-08-03, is the case this closes — `manual` PRs don't get one:
    they're "waiting on your merge", not "broken and getting worse", so there's nothing to
    escalate on)."""
    now = now or datetime.now(timezone.utc)
    parts = []
    for pr, bucket in items:
        key = "#%d:%s" % (pr.number, bucket)
        if bucket == "stuck":
            age = _stuck_age_bucket(pr, now)
            if age:
                key += ":%dd" % age
        parts.append(key)
    return ",".join(sorted(parts))


def should_notify(prev_fp: str, cur_fp: str) -> tuple[bool, str]:
    if cur_fp == prev_fp:
        return False, "none"
    if cur_fp == "":
        return True, "cleared"
    return True, "digest"


def _pr_note(pr: PR) -> str:
    if pr.conflicting:
        return "⚠️ conflicting"
    if pr.ci == "failure":
        return "❌ CI failing"
    if pr.ci == "pending":
        return "⏳ CI pending"
    return "✅ green"


def render_digest(items: list[tuple[PR, str]], limit: int = 1900) -> str:
    total = len(items)
    head = "📦 Renovate — %d PR(s) need attention" % total
    # Build per-PR entries in bucket order; add as many as fit, count the remainder.
    entries: list[tuple[str, list[str]]] = []  # (bucket_header, [lines]) groups
    for bucket in _BUCKET_ORDER:
        group = [(pr) for pr, b in items if b == bucket]
        if not group:
            continue
        lines = []
        for pr in group:
            lines.append(" • #%d %s — %s" % (pr.number, pr.title, _pr_note(pr)))
            lines.append("   %s" % pr.url)
        entries.append((_BUCKET_HEADER[bucket], lines))

    out = [head, ""]
    shown = 0
    truncated = False
    for header, lines in entries:
        block = [header] + lines + [""]
        # +len for a possible "…and N more" tail keeps us safely under the limit.
        if len("\n".join(out + block)) > limit - 20:
            truncated = True
            break
        out += block
        shown += len(lines) // 2
    msg = "\n".join(out).rstrip()
    if truncated and shown < total:
        msg += "\n…and %d more" % (total - shown)
    return msg
