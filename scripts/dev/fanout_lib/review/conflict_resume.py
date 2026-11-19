"""The review pipeline's answer to a landing that ends `merge-conflict`: one resume, then a re-land.

WHY. `land.sh` ends `VERDICT: merge-conflict` when master moved under the PR. It merged and
deployed nothing, and it re-runs safely once the branch is brought up to master. A batch that
ended `needs input:` there left nobody to do that: the unit was gone, and a coordinator edits
nothing, so each conflict cost a hand-spawned agent. Three of the first six PRs of the
2026-11-19 coordinator run ended this way.

THE RESUME. The implementer is resumed once, as the `conflict` phase, with the conflicting
files named. It rebases the branch onto the base, resolves the conflicts, runs its tests and
pushes with `--force-with-lease`. It does not land.

DECIDED: the conflict resume rebases, and the pipeline lands again only after a delta review.
The repo prefers a rebase to a merge for a pushed PR branch, and auto mode clears the
`--force-with-lease` push. A resolution is code no reviewer read, so a fresh reviewer reads
what the rebase changed in the PR's own commits (`git range-diff`) as the `review-conflict`
phase. `record.head` moves to the rebased head only once that review has read it. A
confident finding of medium severity or worse holds the PR, as one left after the fix round
does. A review that fails holds it too.

THE BOUND. One resume per batch, and only with `LAND_MARGIN_S` of run time left, checked
before the resume and again before the re-land. A second conflict, too little time, a
resume that pushed nothing and a batch with no session to resume all end `needs input:`
with the conflicting files named.

A daniel-server batch that lands its own draft takes the same path. Its rebased head is put
to `review_ci.server_lands` again, as a CI fix's head is.
"""

from functools import partial
from typing import TYPE_CHECKING

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.review.delta_confirm import split_confirmed
from fanout_lib.review.review_ci import awaits_ci, land_draft, pr_head, server_lands
from fanout_lib.review.review_land import (
    LAND_MARGIN_S,
    MERGE_CONFLICT,
    Landing,
    out_of_time,
)
from fanout_lib.review.review_prompts import conflict_prompt, conflict_review_prompt
from fanout_lib.review.review_record import (
    actionable,
    findings_of,
    held_for_findings,
    issues_section,
)

if TYPE_CHECKING:
    from fanout_lib.review.review import Pipeline

# The most conflicting files a report or a prompt names; the rest are counted.
MAX_NAMED = 20


def conflicting_files(pipe: "Pipeline") -> list[str]:
    """The files in which the batch's branch conflicts with its base, after a fetch of it.

    Empty when the two merge cleanly, and when git could not say.
    """
    base = pipe.target.base
    pipe._git("fetch", "--quiet", "origin", base.split("/", 1)[1])
    merged = pipe.run(
        ["git", "-C", str(pipe.worktree), "merge-tree", "--write-tree", "--name-only",
         "--no-messages", base, "HEAD"],
        None,
    )  # fmt: skip
    # Exit 1 is a merge with conflicts: the tree's id, then one conflicting path per line.
    if merged.returncode != 1:
        return []
    return [line for line in merged.stdout.splitlines()[1:] if line]


def _named(files: list[str]) -> str:
    if not files:
        return "git merge-tree named none"
    more = len(files) - MAX_NAMED
    return ", ".join(files[:MAX_NAMED]) + (f" and {more} more" if more > 0 else "")


def _stopped(pr: str, landing: Landing, files: list[str], why: str) -> dict:
    text = (
        f"needs input: the landing of {pr} stopped short on a merge conflict, {why}. "
        f"Conflicting files: {_named(files)}.\n{landing.line}"
    )
    return {"type": "result", "is_error": False, "result": f"{text}\n{pr}"}


def _review(pipe: "Pipeline", files: list[str], before: str, after: str) -> dict | None:
    """Have a fresh reviewer read the resolution; a `needs input:` report if it holds the PR."""
    record = pipe.record
    old_base = pipe.range_base
    pipe.range_base = pipe._git("merge-base", after, pipe.target.base)
    delta = pipe._claude(
        "review-conflict",
        pipe._reviewer(),
        conflict_review_prompt(
            issues_section(pipe.brief), old_base, before, pipe.range_base, after, files
        ),
    )
    left, error = findings_of(delta)
    if left is None:
        record.review_error = f"conflict review: {error}"
        why = f"the review of the conflict resolution did not complete ({error})"
        return {
            "type": "result",
            "is_error": False,
            "result": f"needs input: {why}, so the PR was not landed again.\n{record.pr}",
        }
    confirmed, unverified = split_confirmed(
        actionable(left), partial(pipe._read_at, after)
    )
    record.remaining += confirmed
    record.unverified += unverified
    record.head = after
    return held_for_findings(record, pipe.state_dir, "the conflict resolution")


def land_resolving(pipe: "Pipeline") -> tuple[Landing, dict | None]:
    """Land the batch's draft (`land_draft`), resuming the implementer once for a conflict.

    Returns the landing that ended last, and the `needs input:` report that ends the batch
    when one does; else None.
    """
    landing, stopped = land_draft(pipe)
    if stopped or landing.verdict != MERGE_CONFLICT:
        return landing, stopped
    record = pipe.record
    files = conflicting_files(pipe)
    if not pipe.session:
        return landing, _stopped(record.pr, landing, files, "with no session to resume")
    if pipe.time_left() < LAND_MARGIN_S:
        why = "with too little of the unit's run time left to resolve it and land again"
        return landing, _stopped(record.pr, landing, files, why)
    before = record.head
    prompt = conflict_prompt(record.pr, landing.line, files, pipe.target.base)
    pipe._claude("conflict", pipe._resume(), prompt)
    after = pipe._git("rev-parse", "HEAD")
    if after == before or pr_head(pipe.run, record.pr, pipe.target.repo) != after:
        why = "and the one resume for it pushed no new head"
        return landing, _stopped(record.pr, landing, files, why)
    held = _review(pipe, files, before, after)
    if held:
        return landing, held
    if awaits_ci(pipe.host, pipe.target) and not server_lands(pipe, after):
        why = f"and its resolution left the PR for a person: {record.land_hold}"
        return landing, _stopped(record.pr, landing, files, why)
    if pipe.time_left() < LAND_MARGIN_S:
        return landing, out_of_time(record.pr)
    landing, stopped = land_draft(pipe)
    if not stopped and landing.verdict == MERGE_CONFLICT:
        files = conflicting_files(pipe)
        why = "again after the one resume for it"
        return landing, _stopped(record.pr, landing, files, why)
    return landing, stopped
