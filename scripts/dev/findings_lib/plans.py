"""What `findings.py` decides to do, as gh argv nobody has run yet.

Every command in `findings.py` plans first and runs second, so the decision — file, touch,
reopen, refuse, escalate, close — is a pure function of the issue it was handed. `--dry-run`
prints what these return; `gh_calls.run` is the only thing that executes them.
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from datetime import date
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Mapping

from dev.findings_lib.milestone import branch_of, is_project
from dev.findings_lib.issue_model import (
    LABELS,
    NO_REOPEN,
    NOT_PLANNED_PREFIX,
    NOT_BEFORE_PREFIX,
    PR_REPO,
    _REOBSERVED,
    Blocker,
    Issue,
    claim_comment,
    fold_marker,
    release_comment,
    reobservations,
    trailer,
    verify_by_section,
)


def plan_sync_labels(existing: set[str]) -> list[list[str]]:
    plans = []
    for name, (colour, desc) in LABELS.items():
        if name not in existing:
            plans.append(
                ["label", "create", name, "--color", colour, "--description", desc]
            )
    return plans


def plan_defer(issue: Issue, *, until: date | None) -> list[list[str]]:
    """Plans the gh argv that records setting, moving or clearing ``issue``'s not-before date.

    The date itself is the Not Before issue field, which the caller sets with
    `gh_calls.set_fields`. This plans the comment the thread reads later, and removes every
    `not-before:` label still on the issue: `not_before` takes the latest of the field and
    the labels, so a label left behind would outlive a move to an earlier date or a clear.

    Args:
        until: the first day the issue may be offered again, or None to clear the deferral.

    Raises:
        ClaimRefused: the issue is closed, or clearing an issue that carries no deferral.
    """
    if not issue.is_open:
        raise ClaimRefused("closed — nothing to defer")
    if until is None and issue.not_before is None:
        raise ClaimRefused("not deferred — no Not Before date to clear")
    n = str(issue.number)
    stale = sorted(x for x in issue.labels if x.startswith(NOT_BEFORE_PREFIX))
    plans = [["issue", "edit", n, *_flat("--remove-label", stale)]] if stale else []
    note = (
        "Deferral cleared." if until is None else f"Deferred until {until.isoformat()}."
    )
    plans.append(["issue", "comment", n, "--body", note])
    return plans


def plan_block(issue: Issue, *, on: list[str], clear: bool) -> list[list[str]]:
    """Plans the gh argv that marks ``issue`` blocked by other issues, or removes that.

    Writes GitHub's native "blocked by" relationship. `next` and `claim` withhold the issue
    while any blocker is open, and it frees itself when the last one closes, so a block
    never needs clearing for that.

    Args:
        on: the blocking issues, each a number or an issue URL (another repository's
            issue needs the URL). With ``clear``, an empty list removes every blocker.
        clear: remove the relationship instead of adding it.

    Raises:
        ClaimRefused: the issue is closed, would block itself, or ``clear`` finds no
            blocker to remove.
    """
    if not issue.is_open:
        raise ClaimRefused("closed — nothing to block")
    n = str(issue.number)
    if not clear:
        return plan_add_blockers(issue.number, on)
    if not on:
        on = [_blocker_ref(b) for b in issue.blockers]
        if not on:
            raise ClaimRefused("not blocked — no blocker to remove")
    return [["issue", "edit", n, "--remove-blocked-by", ",".join(on)]]


def plan_add_blockers(number: int, on: list[str]) -> list[list[str]]:
    """Plans the gh argv that records each of ``on`` as blocking issue ``number``.

    `open` calls it for an issue it has just created; `plan_block` for one it has read.

    Raises:
        ClaimRefused: ``on`` names the issue itself.
    """
    if str(number) in on:
        raise ClaimRefused("an issue cannot block itself")
    return [["issue", "edit", str(number), "--add-blocked-by", ",".join(on)]]


def _blocker_ref(blocker: Blocker) -> str:
    """How `gh issue edit` names a blocker: its number in this repo, its URL elsewhere."""
    if blocker.url.lower().startswith(f"https://github.com/{PR_REPO.lower()}/issues/"):
        return str(blocker.number)
    return blocker.url or str(blocker.number)


def plan_release_held(
    issue: Issue, *, when: str, reason: str, detach: bool = True
) -> list[list[str]]:
    """The gh argv releasing whatever claim ``issue`` carries, or ``[]`` when it carries none.

    Releases whoever holds it, not just the caller's own worktree. `claims`, `reap` and
    `next` all read OPEN issues, so a claim left on a closed one is invisible to every view
    at once — wrong rather than merely stale, and unreapable.

    Shared by `close`, `open`'s reopen path and `manual`. A `Closes #<n>` merge strands
    a claim too, and `plan_open` reopening that issue for a later re-observation brought the
    stale claim back LIVE, blocking `claim` and withholding the issue from `next` for as long
    as the claiming worktree existed.

    ``detach=False`` keeps the issue in its session milestone. `close` passes it, because
    a closed issue is that session's finished work.
    """
    held = issue.holder
    if not held:
        return []
    return plan_release(issue, worktree=held, when=when, reason=reason, detach=detach)


def plan_manual(issue: Issue, *, clear: bool) -> list[list[str]]:
    """Plans the gh argv that reserves ``issue`` for the operator, or hands it back.

    `next` withholds a `manual` issue and `claim` refuses one. Unlike `not-before:`, nothing
    expires it, so `--clear` is the only way back. The comment records the change in the
    thread, as `plan_defer`'s does. Releasing a claim the issue already carries is the
    caller's job (`plan_release_held`), since `reap` skips `manual` issues.

    Raises:
        ClaimRefused: the issue is closed, already `manual`, or `clear` finds no label.
    """
    if not issue.is_open:
        raise ClaimRefused("closed — nothing to mark")
    n = str(issue.number)
    marked = "manual" in issue.labels
    if clear:
        if not marked:
            raise ClaimRefused("not manual — no `manual` label to clear")
        return [
            ["issue", "edit", n, "--remove-label", "manual"],
            [
                "issue",
                "comment",
                n,
                "--body",
                "Manual cleared: `next` may offer it again.",
            ],
        ]
    if marked:
        raise ClaimRefused("already manual")
    return [
        ["issue", "edit", n, "--add-label", "manual"],
        [
            "issue",
            "comment",
            n,
            "--body",
            "Marked manual: reserved for the operator; `next` and `claim` skip it.",
        ],
    ]


def _flat(flag: str, values: list[str]) -> list[str]:
    return [arg for v in values for arg in (flag, v)]


def plan_touch(issue: Issue, source: str) -> list[list[str]]:
    """A re-observation is a comment; the third one adds `escalated`."""
    n = str(issue.number)
    seen = reobservations(issue) + 1
    plans = [
        [
            "issue",
            "comment",
            n,
            "--body",
            f"{_REOBSERVED} by {source} (sighting {seen + 1}).",
        ]
    ]
    if seen >= 2 and "escalated" not in issue.labels:
        plans.append(["issue", "edit", n, "--add-label", "escalated"])
    return plans


def plan_comment(issue: Issue, body: str) -> list[list[str]]:
    """Plans the gh argv that posts ``body`` on ``issue`` as a plain comment.

    It adds no label and counts as no re-observation. A closed issue takes one too, since a
    correction to its record is still worth keeping.

    Raises:
        ClaimRefused: the issue is outside the register, the body is blank, or the body
            carries a marker the register folds back into state (`fold_marker`).
    """
    if "claude" not in issue.labels:
        raise ClaimRefused("not in the register — no `claude` label")
    if not body.strip():
        raise ClaimRefused("the body is empty")
    marker = fold_marker(body)
    if marker is not None:
        raise ClaimRefused(
            f"the body carries {marker}, which the register would read as state; "
            "use the verb that owns it"
        )
    return [["issue", "comment", str(issue.number), "--body", body]]


class ClaimRefused(Exception):
    """The issue will not accept this claim or release.

    Carries the operator-facing reason, which `cmd_claim` prints before exiting 3 — the
    same exit code every other "nothing was written because the issue refuses it" path uses.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def plan_claim(
    issue: Issue,
    *,
    worktree: str,
    session: str | None,
    when: str,
    today: date,
    milestone: str | None = None,
    reserved_by: Mapping[str, object] | None = None,
) -> list[list[str]]:
    """Plans the gh argv to claim ``issue`` for ``worktree``.

    ``milestone`` is the title of the worktree's session milestone, which an issue edit
    files the issue under. None, for a branch that never ran `milestone start`, files it
    under nothing. An issue in another branch's project milestone stays there.

    ``reserved_by`` is the open project that `milestone.reservations` says reserves the
    issue against ``worktree``, or None. A claim ``worktree`` already holds stands.

    The comment is the whole claim. `findings.main` then syncs the issue's board Status to
    In Progress.

    Returns an EMPTY list when ``worktree`` already holds the claim and the issue is filed
    under ``milestone``, so re-running a claim is idempotent rather than a second comment on
    the same thread. A reclaim whose milestone edit failed plans that edit alone.

    Raises:
        ClaimRefused: the issue is closed, lacks the `claude` label, is labelled `manual`,
            carries a Not Before date after ``today``, has an open blocker, sits in
            another branch's open project, or another worktree already holds it.
    """
    if not issue.is_open:
        raise ClaimRefused("closed — nothing to work")
    names = issue.labels
    # An issue outside the register is invisible to `claims`, `reap` and `next` alike: all
    # three read `load_issues`, which filters `--label claude`, while `_load_issue` does not.
    # So a claim on one could only ever be cleared by a hand-typed `release`, and nothing
    # would tell anyone it was there.
    if "claude" not in names:
        raise ClaimRefused(
            "not in the register — no `claude` label, so `claims`, `reap` and `next` "
            "would all be blind to the claim"
        )
    if "manual" in names:
        raise ClaimRefused(
            f"labelled `manual` — reserved for the operator; `manual {issue.number} --clear` lifts it"
        )
    day = issue.not_before
    if day is not None and today < day:
        # The date is the issue's own precondition, so a claim before it is a wasted
        # dispatch. The way out is named, unlike `manual`'s.
        raise ClaimRefused(
            f"deferred until {day.isoformat()} — `defer {issue.number} --clear` lifts it"
        )
    blockers = issue.open_blockers
    if blockers:
        raise ClaimRefused(
            f"blocked by {', '.join(blockers)} — it frees itself when they close; "
            f"`block {issue.number} --clear` lifts it"
        )
    n = str(issue.number)
    filed_in = issue.milestone
    # A hand-steered worker claims from a project's backlog under its own branch.
    in_their_project = is_project(filed_in) and branch_of(filed_in) != worktree
    filed = issue.milestone_title == milestone
    file_under = (
        [["issue", "edit", n, "--milestone", milestone]]
        if milestone and not filed and not in_their_project
        else []
    )
    held = issue.holder
    if held == worktree:
        return file_under
    if reserved_by:
        owner = branch_of(reserved_by)
        raise ClaimRefused(
            f"reserved by project `{reserved_by['title']}` of `{owner}` — only that "
            f"branch claims from its backlog; `milestone remove {n} --worktree {owner}` "
            "takes it out"
        )
    if held:
        raise ClaimRefused(f"already claimed by `{held}`")
    return [
        ["issue", "comment", n, "--body", claim_comment(worktree, session, when)],
        *file_under,
    ]


def plan_release(
    issue: Issue,
    *,
    worktree: str,
    when: str,
    reason: str | None,
    detach: bool = True,
) -> list[list[str]]:
    """Plans the gh argv to release ``worktree``'s claim on ``issue``.

    Takes the issue out of ``worktree``'s session milestone unless ``detach`` is False. A
    milestone the operator set, or another session's, is left alone. So is a project
    milestone, whose backlog the released issue goes back to.

    Raises:
        ClaimRefused: nobody holds the issue, or somebody else holds it. One session
            releasing another's claim is always a mistake, so it is refused rather than
            allowed with a warning.
    """
    n = str(issue.number)
    held = issue.holder
    if held is None:
        raise ClaimRefused("not claimed")
    if held != worktree:
        raise ClaimRefused(f"claimed by `{held}`, not by `{worktree}`")
    filed_in = issue.milestone
    detach_plan = (
        [["issue", "edit", n, "--remove-milestone"]]
        if detach and branch_of(filed_in) == worktree and not is_project(filed_in)
        else []
    )
    return [
        ["issue", "comment", n, "--body", release_comment(worktree, when, reason)],
        *detach_plan,
    ]


def plan_milestone_add(issue: Issue, project: dict) -> list[list[str]]:
    """Plans the gh argv that files ``issue`` in the backlog of the milestone ``project``.

    Returns an empty list when the issue is already there.

    Raises:
        ClaimRefused: the issue is closed, outside the register, claimed, or held by
            another milestone, whoever made that one.
    """
    if not issue.is_open:
        raise ClaimRefused("closed — nothing to work")
    if "claude" not in issue.labels:
        raise ClaimRefused(
            "not in the register — no `claude` label, so `next` is blind to it"
        )
    title = issue.milestone_title
    if title == project["title"]:
        return []
    if title is not None:
        raise ClaimRefused(
            f"in milestone `{title}` — `milestone remove` on its branch frees it"
        )
    held = issue.holder
    if held:
        raise ClaimRefused(f"claimed by `{held}` — a backlog holds unclaimed issues")
    return [["issue", "edit", str(issue.number), "--milestone", project["title"]]]


def plan_milestone_remove(issue: Issue, project: dict) -> list[list[str]]:
    """Plans the gh argv that takes ``issue`` out of the milestone ``project``.

    Raises:
        ClaimRefused: the issue is not in that milestone, or a worktree holds its claim.
    """
    if issue.milestone_title != project["title"]:
        raise ClaimRefused(f"not in `{project['title']}`")
    held = issue.holder
    if held:
        raise ClaimRefused(f"claimed by `{held}` — `release` it first")
    return [["issue", "edit", str(issue.number), "--remove-milestone"]]


def plan_open(
    existing: Issue | None,
    *,
    title: str,
    body: str,
    labels: list[str],
    fp: str,
    source: str,
    verify_by: str | None = None,
) -> tuple[str, int, list[list[str]]]:
    """Plans the gh argv to file, touch or reopen a finding, given its matching issue.

    Args:
        existing: the issue matching this finding's fingerprint, or None if it is new.
        title: issue title.
        body: issue body, before the verify-by section and the fingerprint/source trailer
            are appended.
        labels: labels to apply on create. Of these, only `review-leftover` is also added
            to a matched issue that lacks it.
        fp: the finding's fingerprint.
        source: the review or session that produced this finding.
        verify_by: prose describing how to check whether the finding is fixed, which
            `verify` prints back; stored only when creating a new issue.

    The severity and not-before date are issue fields, which `gh issue create` cannot set,
    so the caller writes them once the issue exists.

    Returns:
        A ``(outcome, exit_code, plans)`` tuple: outcome is one of ``created``, ``touched``,
        ``reopened``, ``refuted`` or ``accepted``; exit_code is 3 for the last two, which
        are the terminal closes nothing reopens; plans is the gh argv list to run.
    """
    if existing is None:
        full_body = body
        if verify_by:
            full_body += verify_by_section(verify_by)
        full_body += trailer(fp, source)
        argv = [
            "issue",
            "create",
            "--title",
            title,
            "--body",
            full_body,
        ]
        for lab in labels:
            argv += ["--label", lab]
        return "created", 0, [argv]
    n = str(existing.number)
    names = existing.labels
    terminal = sorted(NO_REOPEN & names)
    if not existing.is_open and terminal:
        # Refuted means the finding was disproved; accepted means it is true and the operator
        # chose to live with it. Either way re-filing it would reopen a decision already made,
        # so the caller gets exit 3 and no writes are planned.
        return terminal[0], 3, []
    # A review leftover is usually re-filed against the fix round's own issue, so it lands
    # here rather than on the create path, and `next` would rank it as fresh.
    leftover = "review-leftover" in labels and "review-leftover" not in names
    relabel = (
        [["issue", "edit", n, "--add-label", "review-leftover"]] if leftover else []
    )
    if not existing.is_open:
        return (
            "reopened",
            0,
            [
                ["issue", "reopen", n],
                [
                    "issue",
                    "comment",
                    n,
                    "--body",
                    f"{_REOBSERVED} by {source} after it was closed as fixed: treat as a regression.",
                ],
                *relabel,
            ],
        )
    return "touched", 0, plan_touch(existing, source) + relabel


def plan_close(
    number: int,
    *,
    outcome: str,
    pr: int | None = None,
    reason: str | None = None,
    comment: str | None = None,
    register: str | None = None,
    pr_repo: str | None = None,
) -> list[list[str]]:
    """Plans the gh argv to close an issue as fixed, refuted or accepted.

    Args:
        number: the issue number.
        outcome: one of ``fixed`` (closed as completed), ``refuted`` (a skeptic disproved it)
            or ``accepted`` (true, and the operator chose to live with it). The last two
            close as not planned and add a label of the same name.
        pr: the PR that fixed it, included in the close comment when given.
        reason: required for every outcome but ``fixed``; what disproved the finding, or why
            the trade-off was accepted.
        comment: overrides the default close comment, for a caller that has something to
            record other than the PR that fixed the finding.
        register: the issue's `OWNER/NAME`, or None for `PR_REPO`.
        pr_repo: the repo `pr` belongs to, or None for `PR_REPO`. The comment qualifies the PR
            when it differs from the register, because a bare `#N` links the register's own PR N.

    Raises:
        KeyError: if ``outcome`` is not one of the three names.
    """
    n = str(number)
    if outcome == "fixed":
        where = pr_repo or PR_REPO
        ref = (
            f"#{pr}"
            if where.lower() == (register or PR_REPO).lower()
            else f"{where}#{pr}"
        )
        by = f" by PR {ref}" if pr else ""
        text = comment or f"Fixed{by}."
        return [["issue", "close", n, "--reason", "completed", "--comment", text]]
    prefix = NOT_PLANNED_PREFIX[outcome]
    return [
        ["issue", "edit", n, "--add-label", outcome],
        [
            "issue",
            "close",
            n,
            "--reason",
            "not planned",
            "--comment",
            f"{prefix}: {reason}",
        ],
    ]
