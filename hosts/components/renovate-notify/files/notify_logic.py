"""Pure decision logic for the Renovate manual-action notifier (no I/O — unit-tested).

Maps open Renovate PRs to an actionable bucket and decides when to (re)notify, so
the I/O shell (renovate_notify.py) only fetches, persists, and posts.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ci_verdict import CIVerdict
from host_lib import DISCORD_MAX, clamp_discord
from pending_logic import PENDING_HEADER
from renovate_identity import RenovatePR, is_renovate

# Renovate rewrites its Dependency Dashboard issue on every run (~daily on this repo's
# daily schedule + at-any-time security/lockfile runs). If the dashboard goes stale or
# vanishes, the Renovate App or renovate.json is broken and dependency updates have
# silently stopped — and because there are then NO PRs, the PR digest reads as a healthy
# "backlog cleared". 8 days = comfortably past the run cadence without false-firing.
DASHBOARD_STALE_DAYS = 8
DASHBOARD_TITLE = "Dependency Dashboard"


@dataclass(frozen=True)
class PR:
    """One open Renovate PR, as classify_pr / actionable / render_digest consume it.

    Fields carrying non-obvious meaning have their own comment below.
    """

    number: int
    title: str
    url: str
    automerge: bool  # Renovate body says Automerge Enabled
    ci: CIVerdict
    conflicting: bool
    created_at: str = (
        ""  # GitHub's PR `created_at` (ISO-8601), already in the pulls-list payload
    )
    # None means not looked up, which is NOT the same as looked up and found nothing. Only populated
    # for conflicting PRs (one extra API call each).
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
        if it.get("title") == DASHBOARD_TITLE and is_renovate(it.get("user")):
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


# Renovate renders per-dependency lookup failures OUTSIDE the "## Repository Problems"
# section, as a blockquote callout appended after the branch lists
# (`getDepWarningsDashboard` in renovate's lib/workers/repository/errors-warnings.ts). So
# parse_repository_problems is structurally blind to them: the dashboard stays fresh, the
# failing dependency gets no PR, and it simply stops being offered an update, with nothing but
# this callout to say why.
#
# The callout's own header is either `> :warning: **Warning**` (the source above) or
# `> [!WARNING]` (what the hosted app emits), so match the sentence rather than the callout
# marker.
DEP_LOOKUP_MARKER = "renovate failed to look up the following dependencies:"

_BACKTICKED = re.compile(r"`([^`]+)`")

# Prefix on each parsed failure, so a digest line says which of the two sources it came from.
DEP_LOOKUP_PREFIX = "lookup failure: "


def parse_dependency_lookup_failures(body: str) -> set[str]:
    """Parse the dashboard body's dependency-lookup warning callout into a set of problems.

    Renovate lists every failed dependency on ONE line, backtick-wrapped and comma-joined,
    followed by a separate `Files affected:` line. Only the marker line is scanned:
    `Files affected` carries backticked spans too, and sweeping the whole callout would
    fold filenames in as if they were failing dependencies.

    Returns each failure prefixed with DEP_LOOKUP_PREFIX. Absent callout -> empty set.
    """
    for line in (body or "").splitlines():
        lowered = line.lower()
        if DEP_LOOKUP_MARKER not in lowered:
            continue
        tail = line[lowered.index(DEP_LOOKUP_MARKER) + len(DEP_LOOKUP_MARKER) :]
        return {DEP_LOOKUP_PREFIX + dep.strip() for dep in _BACKTICKED.findall(tail)}
    return set()


def find_dashboard_problems(issues: list[dict]) -> set[str]:
    """Union of the dashboard's two silent-stall signals, both read off the same issue body.

    The "## Repository Problems" section (see parse_repository_problems) and the
    dependency-lookup warning callout (see parse_dependency_lookup_failures). Renovate
    renders them independently and either can appear alone. Empty set when the dashboard is
    absent or carries neither.
    """
    issue = _find_dashboard_issue(issues)
    if issue is None:
        return set()
    body = issue.get("body") or ""
    return parse_repository_problems(body) | parse_dependency_lookup_failures(body)


def problems_fingerprint(problems: set[str]) -> str:
    """Dedupe key for the Repository Problems bucket.

    Sorted so ordering is stable, but the problem strings themselves are the key — a persistent
    problem set re-notifies only on change, while a NEW problem (even alongside old ones) changes
    the string and re-pages.
    """
    return ",".join(sorted(problems))


PROBLEMS_HEADER = "Renovate — Repository Problems (updates silently stalled):"


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


def is_dead_path(pr: PR) -> bool:
    """True when a conflicting PR edits ONLY files that no longer exist on the base branch.

    This is the difference between a PR that needs a rebase and one that can never be rebased.
    Renovate holds one branch per branchName, so a branch stuck against a deleted path blocks the
    dependency it tracks from ever getting a mergeable PR — while the dashboard still detects the
    update at the LIVE path and reports the PR merely as "conflicting", which reads as ordinary
    rebase noise.

    Such a PR needs closing and recreating, not rebasing, and the plain "conflicting" label is
    what lets it sit for weeks.

    Requires ALL changed files to be gone: a PR touching one live and one deleted path is an
    ordinary conflict a rebase can resolve.
    """
    if not pr.conflicting or not pr.dead_paths:
        return False
    return True


def classify_pr(pr: PR) -> str:
    """Bucket one PR as "dead-path", "manual", "stuck", or "on-track"."""
    if is_dead_path(pr):
        # Ahead of the automerge check: a dead-path PR needs a human whether or not automerge was
        # enabled.
        return "dead-path"
    if not pr.automerge:
        return "manual"
    if pr.ci is CIVerdict.FAIL or pr.conflicting:
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


CLEARED_MSG = "Renovate backlog cleared — nothing needs your attention."

_BUCKET_ORDER = ("stuck", "manual")
_BUCKET_HEADER = {
    "stuck": "Stuck (should auto-merge, can't):",
    "manual": "Awaiting your merge (merging → auto-deploys, health-gated, ≤30 min):",
}


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
    forever while it ages (`manual` PRs don't get one: they're "waiting on your merge", not "broken and getting worse", so there's
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
        # Names the remedy: "conflicting" implies a rebase fixes it, but the files are gone from the
        # base branch.
        return (
            "DEAD PATH: conflicting against deleted path(s) — close it, Renovate recreates: %s"
            % (", ".join(pr.dead_paths or ()),)
        )
    if pr.conflicting:
        return "CONFLICTING"
    if pr.ci is CIVerdict.FAIL:
        return "CI FAILING"
    if pr.ci is CIVerdict.PENDING:
        return "CI pending"
    return "green"


def render_digest(items: list[tuple[PR, str]], limit: int = DISCORD_MAX) -> str:
    """Render the actionable (pr, bucket) list into a Discord digest message.

    Groups by bucket in `_BUCKET_ORDER`, then adds PRs one at a time while they fit under
    `limit` characters, Discord's message cap, and ends with a "…and N more" line for the rest.

    Args:
        items: (pr, bucket) pairs, typically `actionable()`'s output.
        limit: character budget for the rendered message.
    """
    total = len(items)
    out = ["Renovate — %d PR(s) need attention" % total, ""]
    shown = 0
    truncated = False
    for bucket in _BUCKET_ORDER:
        group = [pr for pr, b in items if b == bucket]
        if not group or truncated:
            continue
        header = [_BUCKET_HEADER[bucket]]
        for pr in group:
            entry = [
                " • #%d %s — %s" % (pr.number, pr.title, _pr_note(pr)),
                "   %s" % pr.url,
            ]
            # The 20 spare characters hold a possible "…and N more" tail.
            if len("\n".join(out + header + entry)) > limit - 20:
                truncated = True
                break
            out += header + entry
            header = []
            shown += 1
        if not header:
            out.append("")
    msg = "\n".join(out).rstrip()
    if truncated and shown < total:
        msg += "\n…and %d more" % (total - shown)
    return msg


# Every top-level section Renovate renders. Used only by the fail-loud check below: an absent
# PENDING_HEADER is ambiguous between "nothing is pending" (healthy) and "Renovate renamed the
# section" (silently inert), and a body carrying NONE of these is the second case.
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


def dashboard_headers_unrecognized(body: str) -> bool:
    """True when a non-empty dashboard body carries none of `KNOWN_DASHBOARD_HEADERS`.

    `parse_pending` matches a section header, the shape that returns an empty set after an
    upstream rename and reads as all-clear forever. The runtime non-vacuity check: an
    unparseable dashboard is reported, not read as healthy.
    """
    if not (body or "").strip():
        return True
    return not any(h in body for h in KNOWN_DASHBOARD_HEADERS)


def dashboard_body(issues: list[dict]) -> str | None:
    """The dashboard issue's raw body, or None when no dashboard issue exists.

    Distinct from `""`: an empty body is a parse problem, an absent dashboard is already
    reported by `dashboard_stale`.
    """
    issue = _find_dashboard_issue(issues)
    return None if issue is None else (issue.get("body") or "")


DASHBOARD_UNPARSEABLE_MSG = (
    "Renovate — the Dependency Dashboard no longer parses: either no section header "
    "matched, or Pending Status Checks yielded no items. Renovate may have renamed a "
    "header or an item marker, which leaves the Repository-Problems and stuck-pending "
    "checks silently inert. Check https://github.com/%s/issues/3"
)


# Renovate closes a PR whose branch it no longer wants and appends this to the title.
AUTOCLOSED_SUFFIX = " - autoclosed"
# One weekly schedule window ("before 6am on monday") plus a day of slack. Renovate reuses a
# branch name for the next update of the same dependency, so an older autoclose on a branch
# that is legitimately awaiting its schedule again would otherwise page forever.
AUTOCLOSED_LOOKBACK_DAYS = 8
# Every dashboard row carries its branch in an HTML comment: `unschedule-branch=` under Awaiting
# Schedule, `unpend-branch=` under Pending Status Checks, and so on. Matched on any prefix,
# because Renovate has renamed one of these before (approvePr- -> unpend-, see PENDING_MARKERS).
_BRANCH_MARKER = re.compile(r"<!-- [A-Za-z]+-branch=(\S+) -->")


def dashboard_branches(body: str) -> set[str]:
    """Every branch named by a row marker anywhere in the dashboard body."""
    return set(_BRANCH_MARKER.findall(body or ""))


@dataclass(frozen=True)
class Autoclosed:
    """One Renovate PR closed unmerged by Renovate whose update the dashboard still lists."""

    number: int
    title: str
    url: str
    branch: str


def find_autoclosed(
    closed_pulls: list[dict],
    open_branches: set[str],
    body: str,
    now: datetime | None = None,
    lookback_days: int = AUTOCLOSED_LOOKBACK_DAYS,
) -> list[Autoclosed]:
    """Autoclosed Renovate PRs whose update went back onto the dashboard.

    Renovate can autoclose a PR hours after opening it, inside the same schedule, and the
    update then returns to "Awaiting Schedule" for a week. None of the other arms can see that: the
    PR is no longer open, the dashboard keeps updating, and an awaiting item is not a problem.
    A PR is reported when Renovate closed it unmerged with the autoclosed title inside the
    lookback window, the dashboard still names its branch, and no open PR carries that branch.

    Args:
        closed_pulls: entries from the closed-pulls list payload.
        open_branches: `head.ref` of every open PR; a reopened or re-raised PR clears the item.
        body: the dashboard issue body.
        now: the reference time, injectable for tests.
        lookback_days: how recently the PR must have closed.
    """
    now = now or datetime.now(timezone.utc)
    listed = dashboard_branches(body)
    out = []
    for pr in closed_pulls:
        head = RenovatePR.from_rest(pr)
        title = (pr.get("title") or "").strip()
        closed_at = pr.get("closed_at") or ""
        if (
            not head.is_renovate
            or pr.get("merged_at")
            or not title.endswith(AUTOCLOSED_SUFFIX)
            or not closed_at
            or head.head_ref not in listed
            or head.head_ref in open_branches
        ):
            continue
        closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if (now - closed).total_seconds() > lookback_days * 86400:
            continue
        out.append(
            Autoclosed(head.number, title, pr.get("html_url", ""), head.head_ref)
        )
    return sorted(out, key=lambda a: a.number)


def autoclosed_fingerprint(items: list[Autoclosed]) -> str:
    """Dedupe key: the PR numbers, so a standing autoclose pages once and a new one re-pages."""
    return ",".join("#%d" % a.number for a in items)


AUTOCLOSED_HEADER = (
    "Renovate autoclosed these PRs unmerged, and their updates are back on the "
    "dashboard. Renovate re-raises them only in the rule's schedule window. Land the bump by "
    "hand, or tick its dashboard checkbox:"
)


def render_autoclosed(items: list[Autoclosed]) -> str:
    lines = [AUTOCLOSED_HEADER] + [
        " • #%d %s — %s" % (a.number, a.title.removesuffix(AUTOCLOSED_SUFFIX), a.url)
        for a in items
    ]
    return "\n".join(lines)


DIGEST_FLOOR = 600


def compose_message(
    sections: list[str], items: list[tuple[PR, str]], limit: int = DISCORD_MAX
) -> str:
    """One Discord message: the report `sections`, then the PR digest of `items`, within `limit`.

    The digest is rendered last and within the room the sections leave, but never less than
    `DIGEST_FLOOR`. When the sections leave less, they are cut to make room, so the cap never
    clips the digest's tail.
    """
    prefix = "\n\n".join(sections)
    if not items:
        return clamp_discord(prefix, limit)
    if not prefix:
        return render_digest(items, limit)
    digest = render_digest(items, max(limit - len(prefix) - 2, DIGEST_FLOOR))
    return clamp_discord(prefix, limit - len(digest) - 2) + "\n\n" + digest
