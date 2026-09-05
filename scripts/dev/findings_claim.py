#!/usr/bin/env python3
"""Whether a claim on an issue is still live, decided from the worktree that holds it.

A claim names a worktree. The question "is this claim still live" is therefore the question
"is this worktree still doing the work", which `prune_worktrees.py` already answers for a
different caller — so this module reuses its judgment rather than adding a second one.

WHY NOT A HEARTBEAT, AND WHY NOT A TTL. Both key on the CLAIMING PROCESS. On 2026-09-05 the
container restarted and killed 14 agents mid-work; every one of their worktrees kept its
uncommitted edits, so each session was resumed in place rather than restarted. A heartbeat
or a TTL would have expired those claims while the work was still live, handing half-finished
issues to a second agent. Keying on the worktree gets that case right with no timer at all:
a worktree with uncommitted changes is holding work, whatever happened to the process.
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from dev.findings_model import current_claim
from dev.prune_worktrees import REMOVABLE, Worktree, classify


@dataclass(frozen=True)
class ClaimState:
    """One issue's claim, and whether the worktree behind it is still working.

    Attributes:
        number: the GitHub issue number.
        worktree: the branch name claiming this issue.
        live: whether the claim is still live (the worktree is still doing the work).
        reason: why the verdict went the way it did, in the words a `claims` row prints.
        age_days: whole days since the claim comment was posted, or None when the comment
            carries no `createdAt` — which is the case in every fixture that builds one by
            hand, so the renderer must handle it.
    """

    number: int
    worktree: str
    live: bool
    reason: str
    age_days: int | None


def claim_is_live(
    worktree_name: str,
    trees: list[Worktree],
    dirty: Callable[[str], bool],
    merged: Callable[[Worktree], bool],
) -> tuple[bool, str]:
    """Whether the claim held by ``worktree_name`` is still live.

    Args:
        worktree_name: the branch name recorded in the claim comment.
        trees: every registered worktree, from `prune_worktrees.parse_worktree_list`.
        dirty: takes a worktree path, returns whether it has uncommitted changes.
        merged: takes a Worktree, returns whether it is merged into origin/master. It
            takes the TREE and not the branch name because `is_merged` keys on the head SHA;
            passing a branch name as its `head` argument makes every git layer exit non-zero
            and the function return False for everything.

    Returns:
        (live, reason). The reason is `classify`'s own, so a `claims` row and a
        `prune_worktrees` row say the same thing about the same worktree.

    DELEGATES TO `classify`. Re-implementing the verdict here would drop a condition, and
    it did in an earlier draft of this plan: the live-session-lock check. An orchestrator
    worktree that only orchestrates has no commits and no edits, so its HEAD is an ancestor
    of origin/master and `is_merged` says True — every claim it makes would read as stale
    the moment it was written, and `reap` would release it while the fan-out was running.
    `classify` checks the live lock FIRST, which is exactly what that case needs.
    """
    tree = next((t for t in trees if t.branch == worktree_name), None)
    if tree is None:
        return False, "no worktree — the claim names a branch nothing has checked out"
    verdict, reason = classify(tree, merged=merged(tree), dirty=dirty(tree.path))
    return verdict != REMOVABLE, reason


def claim_states(
    issues: list[dict],
    trees: list[Worktree],
    dirty: Callable[[str], bool],
    merged: Callable[[Worktree], bool],
) -> list[ClaimState]:
    """One `ClaimState` per issue that is currently claimed, in issue order."""
    states = []
    for issue in issues:
        held = current_claim(issue)
        if not held:
            continue
        live, reason = claim_is_live(held, trees, dirty, merged)
        states.append(
            ClaimState(
                issue["number"], held, live, reason, _claim_age_days(issue, held)
            )
        )
    return states


def _claim_age_days(issue: dict, held: str) -> int | None:
    """Whole days since the comment that opened ``held``'s claim, or None.

    Finds the FIRST comment claiming ``held``, matching `current_claim`'s first-writer-wins
    fold. Returns None when the comment carries no parseable `createdAt`, which is how every
    hand-built fixture looks.
    """
    for comment in issue.get("comments", []):
        if f"Claim: `{held}`" not in (comment.get("body") or ""):
            continue
        raw = comment.get("createdAt")
        if not raw:
            return None
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(UTC) - when).days
    return None
