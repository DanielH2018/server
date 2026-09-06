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

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from dev.findings_model import (
    _CLAIM_RE,
    _RELEASE_RE,
    current_claim,
    is_operator_comment,
    label_names,
)
from dev.prune_worktrees import REMOVABLE, Worktree, classify

# What a worktree read raises when the directory behind a registered worktree is gone.
# `lib.git.git_dirty` runs git with `check=True`, so a missing cwd surfaces as OSError from
# subprocess itself, and a git that ran but exited non-zero surfaces as CalledProcessError.
_UNREADABLE = (OSError, subprocess.SubprocessError)


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

    AN UNREADABLE WORKTREE IS HELD, NOT RELEASED. A PRUNABLE worktree — one whose directory
    was removed without `git worktree remove` — keeps appearing in `git worktree list
    --porcelain`, so it reaches here in the ordinary course of events. `dirty` then runs git
    with `check=True` in a directory that does not exist and RAISES, which took `claims`,
    `reap` and `next` down together under an error blaming gh (#1276). Holding the claim is
    the same fail-safe direction a dirty worktree with a dead owner already takes: releasing
    a claim whose state cannot be read hands live work to a second session. The reason names
    `git worktree prune`, because nothing else in the output points at a stale registration.
    """
    tree = next((t for t in trees if t.branch == worktree_name), None)
    if tree is None:
        return False, "no worktree — the claim names a branch nothing has checked out"
    try:
        tree_merged, tree_dirty = merged(tree), dirty(tree.path)
    except _UNREADABLE:
        return True, (
            f"worktree state unreadable — {tree.path} is registered but gone; "
            "run `git worktree prune`"
        )
    verdict, reason = classify(tree, merged=tree_merged, dirty=tree_dirty)
    return verdict != REMOVABLE, reason


def another_claim_blocks(issue: dict, worktree: str) -> bool:
    """Whether a claim by someone else is the only thing standing in ``worktree``'s way.

    Cheap and pure, so `cmd_claim` reads it before paying for `_worktree_facts` — several git
    calls per registered worktree, on a batch where most issues are unclaimed.

    An issue `plan_claim` would refuse anyway answers False even when a claim sits on it:
    `plan_claim` is the single authority on those refusals, and reaping a claim off such an
    issue would post a release to no purpose. There are three — closed, `manual`, and outside
    the `claude` register (#1277) — and this list has to stay level with `plan_claim`'s.
    """
    names = label_names(issue)
    if (
        issue.get("state", "OPEN") != "OPEN"
        or "manual" in names
        or "claude" not in names
    ):
        return False
    held = current_claim(issue)
    return bool(held) and held != worktree


def stale_holder(
    issue: dict,
    trees: list[Worktree],
    dirty: Callable[[str], bool],
    merged: Callable[[Worktree], bool],
) -> tuple[str, str] | None:
    """(holder, why) when this issue's claim is STALE, else None.

    What `cmd_claim` reaps before taking an issue (#1274). Takes the same three facts
    `claim_states` does rather than `_worktree_facts`'s 4-tuple, so the caller keeps the
    decision about what a FAILED git read means — `cmd_claim` leaves the claim standing,
    `cmd_reap` refuses outright, and neither reads a git error as "every worktree is gone".
    """
    held = current_claim(issue)
    if not held:
        return None
    live, why = claim_is_live(held, trees, dirty, merged)
    return None if live else (held, why)


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
    """Whole days since the comment that opened the currently open episode of ``held``'s claim, or None.

    Folds the comment list forward like `current_claim` does, finding the LAST transition
    from None -> held that is still open (not released). Returns None when that comment
    carries no parseable `createdAt`, or when the datetime is naive (missing timezone info).

    Skips comments `is_operator_comment` rejects, for the same reason `current_claim` does
    (#1280) — and it has to skip exactly the same ones, or the two disagree about which claim
    is current and a `claims` row ages a claim the register does not think exists.
    """
    claimed_at_comment: dict | None = None
    currently_held: str | None = None

    for comment in issue.get("comments", []):
        if not is_operator_comment(comment):
            continue
        body = comment.get("body") or ""

        m = _CLAIM_RE.search(body)
        if m:
            worktree = m.group(1)
            if currently_held is None:
                currently_held = worktree
                if worktree == held:
                    claimed_at_comment = comment
            # The same choice `current_claim` makes, and it has to stay the same: a body
            # carrying both trailers holds the claim rather than releasing it. Grep
            # `DECIDED: \`Claim:\` wins over \`Released:\`` in findings_model.py for the
            # reasoning; `test_a_body_carrying_both_trailers_ages_the_claim_from_the_comment_
            # that_opened_it` is what fails if the two diverge.
            continue

        m = _RELEASE_RE.search(body)
        if m and m.group(1) == currently_held:
            currently_held = None

    if claimed_at_comment is None:
        return None

    raw = claimed_at_comment.get("createdAt")
    if not raw:
        return None

    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

    # fromisoformat accepts date-only strings like "2026-09-01", parsing them as naive
    # datetimes. Subtracting a naive datetime from datetime.now(UTC) raises TypeError,
    # which is not caught by ValueError; return None to treat it as unparseable.
    if when.tzinfo is None:
        return None

    return (datetime.now(UTC) - when).days
