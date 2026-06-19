"""Pure decision logic for the Renovate manual-action notifier (no I/O — unit-tested).

Maps open Renovate PRs to an actionable bucket and decides when to (re)notify, so
the I/O shell (renovate_notify.py) only fetches, persists, and posts.
"""
from __future__ import annotations

from dataclasses import dataclass

# check-run conclusions that mean "this will not merge" (besides success/neutral/skipped).
_FAIL_CONCLUSIONS = {
    "failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure",
}


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    url: str
    automerge: bool          # Renovate body says Automerge Enabled
    ci: str                  # "success" | "pending" | "failure"
    conflicting: bool


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
