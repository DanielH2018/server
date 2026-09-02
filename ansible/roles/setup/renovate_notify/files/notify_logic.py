"""Pure decision logic for the Renovate manual-action notifier (no I/O — unit-tested).

Maps open Renovate PRs to an actionable bucket and decides when to (re)notify, so
the I/O shell (renovate_notify.py) only fetches, persists, and posts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Renovate rewrites its Dependency Dashboard issue on every run (~daily on this repo's
# daily schedule + at-any-time security/lockfile runs). If the dashboard goes stale or
# vanishes, the Renovate App or renovate.json is broken and dependency updates have
# silently stopped — and because there are then NO PRs, the PR digest reads as a healthy
# "backlog cleared". 8 days = comfortably past the run cadence without false-firing.
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
    """One open Renovate PR, as classify_pr / actionable / render_digest consume it.

    Fields carrying non-obvious meaning have their own comment below.
    """

    number: int
    title: str
    url: str
    automerge: bool  # Renovate body says Automerge Enabled
    ci: str  # "success" | "pending" | "failure"
    conflicting: bool
    created_at: str = (
        ""  # GitHub's PR `created_at` (ISO-8601), already in the pulls-list payload
    )
    # The PR's changed files, and whether any of them still exists on the base branch. Populated
    # only for conflicting PRs (see build_pr) — it costs one extra API call each, and the question
    # is meaningless for a PR that merges cleanly. `None` = not looked up, which is NOT the same as
    # "looked up and found nothing".
    dead_paths: tuple[str, ...] | None = None


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
    """Parse the dashboard body's "## Repository Problems" section into a set of problem strings.

    Per-package lookup failures, config warnings, etc. Renovate renders each as a
    backtick-wrapped bullet (` - \\`<problem>\\` `) between the header and the next
    top-level `## ` section. This is the bucket the PR digest can't see: a package whose
    lookup starts failing gets no PR and doesn't touch dashboard staleness either (the
    dashboard still updates fine) — it just silently stops receiving updates forever
    (karakeep's gcr.io image, 2026-08). Absent section -> empty set.
    """
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
    """Parse the dashboard issue's Repository Problems section (see parse_repository_problems).

    Empty set when the dashboard is absent or has no problems section.
    """
    issue = _find_dashboard_issue(issues)
    if issue is None:
        return set()
    return parse_repository_problems(issue.get("body") or "")


def problems_fingerprint(problems: set[str]) -> str:
    """Dedupe key for the Repository Problems bucket.

    Sorted so ordering is stable, but the problem strings themselves are the key — a persistent
    problem set re-notifies only on change, while a NEW problem (even alongside old ones) changes
    the string and re-pages.
    """
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
    watches the *notifier*, not Renovate) can't see.
    """
    if not updated_at:
        return True
    now = now or datetime.now(timezone.utc)
    age_days = (
        now - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    ).total_seconds() / 86400
    return age_days > max_age_days


def parse_automerge(body: str) -> bool:
    """True only if Renovate's body explicitly says Automerge Enabled.

    Absent/unknown -> False, so classify_pr() surfaces it as `manual` (fail toward surfacing).
    """
    return "Automerge**: Enabled" in (body or "")


def ci_rollup(check_runs: list[dict], statuses: list[dict]) -> str:
    """Fold the two disjoint GitHub CI sources into one verdict: "failure", "pending", or "success".

    Checks API (check_runs) and the legacy Commit Status API (statuses). Failure precedes
    pending precedes success: a failure in EITHER source counts.
    """
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


def is_dead_path(pr: PR) -> bool:
    """True when a conflicting PR edits ONLY files that no longer exist on the base branch.

    This is the difference between a PR that needs a rebase and one that can never be rebased.
    Renovate holds one branch per branchName, so a branch stuck against a deleted path blocks the
    dependency it tracks from ever getting a mergeable PR — while the dashboard still detects the
    update at the LIVE path and reports the PR merely as "conflicting", which reads as ordinary
    rebase noise.

    Two instances by 2026-08-20: #67/#42/#69 against compose templates the k3s migration archived,
    then #41 against roles/containers/karakeep after the same cutover. Both needed closing and
    recreating, not rebasing, and in both cases the plain "conflicting" label is what let them sit
    for weeks.

    Requires ALL changed files to be gone: a PR touching one live and one deleted path is an
    ordinary conflict a rebase can resolve.
    """
    if not pr.conflicting or not pr.dead_paths:
        return False
    return True


def classify_pr(pr: PR) -> str:
    """Bucket one PR as "dead-path", "manual", "stuck", or "on-track"."""
    if is_dead_path(pr):
        # Ahead of the automerge check on purpose: a dead-path PR needs a human whether or not
        # automerge was ever enabled on it, and "manual" would file it with PRs that are merely
        # waiting to be reviewed.
        return "dead-path"
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
        if bucket in ("stuck", "manual", "dead-path"):
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
    """Largest `_STUCK_AGE_THRESHOLDS` value the PR's age has crossed, or 0 if under a day old.

    Also 0 when `created_at` is missing or unparseable (age unknown -> no age dimension, same
    as before).
    """
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
    """Dedupe key for the actionable PR set.

    `stuck` PRs carry a coarse age dimension (`_stuck_age_bucket`) so a PR that's been stuck for a
    while re-pages at each threshold crossing instead of paging once on day 1 and then going silent
    forever while it ages (PR #67, stuck since 2026-08-03, is the case this closes — `manual` PRs
    don't get one: they're "waiting on your merge", not "broken and getting worse", so there's
    nothing to escalate on).
    """
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
    if is_dead_path(pr):
        # Names the remedy, because the whole failure mode is that "conflicting" implies a rebase
        # will fix it and here nothing will: the files are gone from the base branch.
        return (
            "🪦 conflicting against deleted path(s) — close it, Renovate recreates: %s"
            % (", ".join(pr.dead_paths or ()),)
        )
    if pr.conflicting:
        return "⚠️ conflicting"
    if pr.ci == "failure":
        return "❌ CI failing"
    if pr.ci == "pending":
        return "⏳ CI pending"
    return "✅ green"


def render_digest(items: list[tuple[PR, str]], limit: int = 1900) -> str:
    """Render the actionable (pr, bucket) list into a Discord digest message.

    Groups by bucket in `_BUCKET_ORDER`, then truncates the tail (adding a "…and N more"
    line) to stay under `limit` characters — Discord's message cap.

    Args:
        items: (pr, bucket) pairs, typically `actionable()`'s output.
        limit: character budget for the rendered message.
    """
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


# --- Pending Status Checks: updates that soak forever and never get a PR ---------------
#
# The dashboard's "Pending Status Checks" section lists updates Renovate has detected but
# is holding back until their `minimumReleaseAge` soak elapses. An item is supposed to leave
# that section within its soak, as a branch and a PR. Measured 2026-09-02 (issue #886), seven
# items had sat there far past it — grafana/promtail 3.3.0 -> 3.6.11 for 111 days, against a
# 7-day soak — with no PR ever raised, and the ONLY signal was a checkbox in an issue nobody
# reads line by line. That is the same "silence != up-to-date" shape the `lockFileMaintenance`
# and galaxy-collection notes in renovate.json already name.
#
# The mechanism is undetermined: Renovate runs here as the Mend hosted app, whose debug log is
# on developer.mend.io and unreadable from this host. So this measures the SYMPTOM — an item's
# continuous dwell in the section — rather than the cause.
PENDING_HEADER = "## Pending Status Checks"

# Every top-level section Renovate renders on the dashboard. Used only for the fail-loud check
# below: an absent PENDING_HEADER is ambiguous between "nothing is pending" (the healthy, common
# case) and "Renovate renamed the section", which would make this whole check silently inert. A
# body carrying NONE of these is the second case.
KNOWN_DASHBOARD_HEADERS = (
    PENDING_HEADER,
    "## Awaiting Schedule",
    "## Detected Dependencies",
    "## Open",
    "## Pending Approval",
    "## Rate-Limited",
    "## Edited/Blocked",
    "## Errored",
    "## Ignored or Blocked",
    REPOSITORY_PROBLEMS_HEADER,
)

# The two `minimumReleaseAge` values renovate.json actually sets for the non-vulnerability
# updates that land in this section: 3 days for the k8s-plane digest bumps (a re-push of the
# same mutable tag, so the wait only lets a poisoned push be noticed) and 7 days for everything
# else non-major. `test_soak_constants_match_renovate_json` asserts both against renovate.json,
# so a soak change there fails rather than leaving these to drift.
DIGEST_SOAK_DAYS = 3
VERSION_SOAK_DAYS = 7

# Grace added on top of an item's own soak before it counts as stuck. It covers the two
# legitimate reasons an item outlives its soak by a little: the top-level `before 6am` schedule
# means Renovate only acts once a day, and `prHourlyLimit: 4` can defer a PR across several of
# those daily windows when a backlog exists. Seven days is at least seven such windows and 28
# PR slots — far past any honest backlog, and still less than half of promtail's 111 days.
# Deliberately NOT a flat threshold: a digest item at 3+7=10 days and a version item at 7+7=14
# each get an allowance derived from the soak that actually applies to it.
PENDING_GRACE_DAYS = 7


def dashboard_headers_unrecognized(body: str) -> bool:
    """True when a non-empty dashboard body carries none of `KNOWN_DASHBOARD_HEADERS`.

    `parse_pending` finds its subject by matching a section header, which is the shape that
    returns an empty set after an upstream rename and reads as all-clear forever. This is the
    non-vacuity check at runtime: an unparseable dashboard is reported, not read as healthy.
    """
    if not (body or "").strip():
        return True
    return not any(h in body for h in KNOWN_DASHBOARD_HEADERS)


def dashboard_body(issues: list[dict]) -> str | None:
    """The dashboard issue's raw body, or None when no dashboard issue exists.

    Distinct from `""`: an existing dashboard with an empty body is a parse problem, while an
    absent dashboard is already reported by `dashboard_stale`.
    """
    issue = _find_dashboard_issue(issues)
    return None if issue is None else (issue.get("body") or "")


def parse_pending(body: str) -> dict[str, str]:
    """Parse the dashboard's Pending Status Checks section into {branch: item description}.

    Each item is rendered as ` - [ ] <!-- approvePr-branch=<branch> -->Update foo to v1.2.3`.
    Keyed on the BRANCH rather than the description on purpose: the branch is stable across
    target-version changes, while the description carries the target. Nine of the 22 items live
    on 2026-09-02 were `:latest`/`:release` Docker DIGEST bumps, whose description changes every
    time upstream re-pushes — keying on description would reset their clock forever and leave the
    check structurally unable to fire on the fastest-churning half of the section.

    Absent section -> empty dict, which is the healthy common case (see
    `dashboard_headers_unrecognized` for the ambiguity that covers).
    """
    if PENDING_HEADER not in (body or ""):
        return {}
    section = body.split(PENDING_HEADER, 1)[1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for line in section.splitlines():
        line = line.strip()
        if "approvePr-branch=" not in line:
            continue
        branch = line.split("approvePr-branch=", 1)[1].split("-->", 1)[0].strip()
        desc = line.split("-->", 1)[1].strip() if "-->" in line else ""
        if branch:
            out[branch] = desc
    return out


def item_soak_days(description: str) -> int:
    """The `minimumReleaseAge` that applies to one pending item, read from its description.

    Renovate writes "Docker digest to <sha>" for a digest bump and "... tag to vX" / "dependency
    X to vY" for a version bump, and renovate.json soaks those for 3 and 7 days respectively.
    Anything unrecognised gets the LONGER soak: a misread must delay the alert, never invent one.

    The 1-day `vulnerabilityAlerts` soak is deliberately not modelled: nothing in the item text
    distinguishes a CVE-driven bump, and those are scheduled "at any time" so they should never
    linger here. A CVE bump that does get stuck waits the 7+7 version allowance like any other.
    """
    return (
        DIGEST_SOAK_DAYS
        if "digest to" in (description or "").lower()
        else VERSION_SOAK_DAYS
    )


def update_pending_seen(
    prev: dict[str, float], current: dict[str, str], now_epoch: float
) -> dict[str, float]:
    """Carry each still-pending item's first-seen epoch forward; stamp new ones; drop departed ones.

    Pruning is what makes the dwell continuous rather than cumulative: an item that leaves the
    section (its PR was finally raised, or the update stopped being offered) and comes back later
    starts a fresh clock, so a resolved stall cannot re-page off its old timestamp.
    """
    return {branch: prev.get(branch, now_epoch) for branch in current}


def stale_pending(
    seen: dict[str, float],
    current: dict[str, str],
    now_epoch: float,
    grace_days: int = PENDING_GRACE_DAYS,
) -> list[tuple[str, str, int]]:
    """(branch, description, whole days pending) for every item past its soak + `grace_days`.

    Sorted longest-pending first, so the digest names the worst offender before any truncation.
    An item with no first-seen entry is treated as first seen now (dwell 0) rather than as
    infinitely old — the first run after this ships has an empty state file, and seeding it must
    not page for all 22 items at once.
    """
    out = []
    for branch, desc in current.items():
        days = (now_epoch - seen.get(branch, now_epoch)) / 86400
        if days > item_soak_days(desc) + grace_days:
            out.append((branch, desc, int(days)))
    return sorted(out, key=lambda item: (-item[2], item[0]))


def pending_fingerprint(items: list[tuple[str, str, int]]) -> str:
    """Dedupe key for the stuck-pending set.

    Carries each item's whole-week dwell so a still-stuck item re-pages weekly instead of once,
    the same escalation `_stuck_age_bucket` gives a stuck PR. Keyed on branch, not description,
    for the reason `parse_pending` is.
    """
    return ",".join(
        sorted("%s:%dw" % (branch, days // 7) for branch, _desc, days in items)
    )


PENDING_HEADER_MSG = (
    "\U0001f6d1 Renovate — update(s) stuck in Pending Status Checks (soaked, no PR):"
)

DASHBOARD_UNPARSEABLE_MSG = (
    "⚠️ Renovate — the Dependency Dashboard body matched none of the section headers "
    "this notifier parses. Renovate may have renamed them, which would leave the "
    "Repository-Problems and stuck-pending checks silently inert. Check "
    "https://github.com/%s/issues/3"
)


PENDING_REMEDY = (
    "   Tick its box on the Dependency Dashboard to force the PR: the update is "
    "detected but Renovate is not raising it."
)


def render_pending(items: list[tuple[str, str, int]], limit: int = 1500) -> str:
    """Render the stuck-pending list into a Discord message, truncated to `limit` characters.

    Bounded for the same reason `render_digest` is, and more urgently: this section held 22
    items on 2026-09-02, and one line runs to ~145 characters, so an unbounded render of a
    fully stuck section is ~3,200 — past Discord's 2,000-character cap. An over-long post is
    rejected, `discord()` returns False, the dedupe fingerprint is never advanced, and the run
    re-posts the same oversized message every day. The check would fail to deliver in exactly
    the state it exists to report.

    `limit` is lower than render_digest's 1900 because both can be joined into one message.
    `stale_pending` sorts worst-offender-first, so the item that matters most survives the trim.
    """
    out = [PENDING_HEADER_MSG]
    shown = 0
    for branch, desc, days in items:
        line = " • %s — pending %d days (%s)" % (desc or branch, days, branch)
        # Leave room for the "…and N more" tail and the remedy line.
        if len("\n".join(out + [line, PENDING_REMEDY])) > limit - 30:
            break
        out.append(line)
        shown += 1
    if shown < len(items):
        out.append("…and %d more" % (len(items) - shown))
    out.append(PENDING_REMEDY)
    return "\n".join(out)
