"""What `findings.py` decides to do, as gh argv nobody has run yet.

Every command in `findings.py` plans first and runs second, so the decision — file, touch,
reopen, refuse, escalate, close — is a pure function of the issue it was handed. `--dry-run`
prints what these return; `gh_calls.run` is the only thing that executes them.

`is_project_failure` and `without_project` are the exception that proves the shape: they are
also pure, and they let `gh_calls._create_with_optional_project` decide whether a `gh`
failure is worth retrying without ever looking at gh itself.
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import (
    LABELS,
    NO_REOPEN,
    PROJECT_TITLE,
    _REOBSERVED,
    claim_comment,
    current_claim,
    label_names,
    release_comment,
    reobservations,
    trailer,
    verify_by_section,
)

# The close comment each not-planned outcome opens with, keyed by the outcome name — which is
# also the label name, so both argv come from the same key. A dict rather than an `if` so a
# typo raises KeyError instead of planning the wrong write.
_NOT_PLANNED_PREFIX = {"refuted": "Refuted", "accepted": "Accepted"}


def plan_sync_labels(existing: set[str]) -> list[list[str]]:
    plans = []
    for name, (colour, desc) in LABELS.items():
        if name not in existing:
            plans.append(
                ["label", "create", name, "--color", colour, "--description", desc]
            )
    return plans


def plan_touch(issue: dict, source: str) -> list[list[str]]:
    """A re-observation is a comment; the third one adds `escalated`."""
    n = str(issue["number"])
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
    if seen >= 2 and "escalated" not in label_names(issue):
        plans.append(["issue", "edit", n, "--add-label", "escalated"])
    return plans


class ClaimRefused(Exception):
    """The issue will not accept this claim or release.

    Carries the operator-facing reason, which `cmd_claim` prints before exiting 3 — the
    same exit code every other "nothing was written because the issue refuses it" path uses.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def plan_claim(
    issue: dict, *, worktree: str, session: str | None, when: str
) -> list[list[str]]:
    """Plans the gh argv to claim ``issue`` for ``worktree``.

    Returns an EMPTY list when ``worktree`` already holds the claim AND the `claimed` label
    is on, so re-running a claim is idempotent rather than a second comment on the same
    thread. A reclaim whose label is MISSING plans the label edit alone — see below.

    LABEL-AWARE, IN BOTH DIRECTIONS (#1277). The comment is the claim and the label is
    decorative, so the two can disagree: `cmd_claim` posts the comment first, and a failed
    `--add-label` (rate limit, transient 502) exits 1 with the claim held and the label off.
    Returning `[]` for every reclaim made that unrepairable — the retry short-circuited
    before the label plan, so the label could never be added. The damage is silent and
    permanent: a wrong answer for anyone filtering GitHub by `label:claimed`.

    Raises:
        ClaimRefused: the issue is closed, lacks the `claude` label, is labelled `manual`,
            or another worktree already holds it.
    """
    if issue.get("state", "OPEN") != "OPEN":
        raise ClaimRefused("closed — nothing to work")
    names = label_names(issue)
    # An issue outside the register is invisible to `claims`, `reap` and `next` alike: all
    # three read `load_issues`, which filters `--label claude`, while `_load_issue` does not.
    # So a claim on one could only ever be cleared by a hand-typed `release`, and nothing
    # would tell anyone it was there (#1277).
    if "claude" not in names:
        raise ClaimRefused(
            "not in the register — no `claude` label, so `claims`, `reap` and `next` "
            "would all be blind to the claim"
        )
    if "manual" in names:
        raise ClaimRefused("labelled `manual` — reserved for the operator")
    n = str(issue["number"])
    add_label = ["issue", "edit", n, "--add-label", "claimed"]
    held = current_claim(issue)
    if held == worktree:
        # The repair. A fresh claim below re-adds the label unconditionally, because
        # `--add-label` on a label already present is a no-op; here the plan IS the repair,
        # so an already-correct label has to plan nothing or the reclaim stops being a no-op.
        return [] if "claimed" in names else [add_label]
    if held:
        raise ClaimRefused(f"already claimed by `{held}`")
    return [
        ["issue", "comment", n, "--body", claim_comment(worktree, session, when)],
        add_label,
    ]


def plan_release(
    issue: dict, *, worktree: str, when: str, reason: str | None
) -> list[list[str]]:
    """Plans the gh argv to release ``worktree``'s claim on ``issue``.

    LABEL-AWARE, the mirror of `plan_claim` (#1277). A release whose `--remove-label` failed
    left the comment posted and the label on, and the retry then raised `ClaimRefused("not
    claimed")` — so a stuck `claimed` label could never be removed. An issue nobody holds
    that still carries the label therefore plans the label edit alone rather than refusing.

    Raises:
        ClaimRefused: nobody holds the issue and no label needs removing, or somebody else
            holds it. One session releasing another's claim is always a mistake, so it is
            refused rather than allowed with a warning.
    """
    n = str(issue["number"])
    remove_label = ["issue", "edit", n, "--remove-label", "claimed"]
    held = current_claim(issue)
    if held is None:
        # The repair: nobody holds it, but the label says somebody does. Removing the label
        # is the whole of what is left to do, so plan that instead of refusing.
        if "claimed" in label_names(issue):
            return [remove_label]
        raise ClaimRefused("not claimed")
    if held != worktree:
        raise ClaimRefused(f"claimed by `{held}`, not by `{worktree}`")
    return [
        ["issue", "comment", n, "--body", release_comment(worktree, when, reason)],
        remove_label,
    ]


def plan_open(
    existing: dict | None,
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
        labels: labels to apply on create.
        fp: the finding's fingerprint.
        source: the review or session that produced this finding.
        verify_by: a read-only command whose exit code later tells `verify` whether the
            finding is fixed; stored only when creating a new issue.

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
        argv += ["--project", PROJECT_TITLE]
        return "created", 0, [argv]
    n = str(existing["number"])
    names = label_names(existing)
    terminal = sorted(NO_REOPEN & names)
    if existing.get("state") == "CLOSED" and terminal:
        # Refuted means the finding was disproved; accepted means it is true and the operator
        # chose to live with it. Either way re-filing it would reopen a decision already made,
        # so the caller gets exit 3 and no writes are planned.
        return terminal[0], 3, []
    if existing.get("state") == "CLOSED":
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
            ],
        )
    return "touched", 0, plan_touch(existing, source)


def plan_close(
    number: int,
    *,
    outcome: str,
    pr: int | None = None,
    reason: str | None = None,
    comment: str | None = None,
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
        comment: overrides the default close comment (`verify` uses this to quote the
            verify-by command and its output instead of naming a PR).

    Raises:
        KeyError: if ``outcome`` is not one of the three names.
    """
    n = str(number)
    if outcome == "fixed":
        by = f" by PR #{pr}" if pr else ""
        text = comment or f"Fixed{by}."
        return [["issue", "close", n, "--reason", "completed", "--comment", text]]
    prefix = _NOT_PLANNED_PREFIX[outcome]
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


def without_project(argv: list[str]) -> list[str]:
    """``argv`` with the ``--project <title>`` pair removed.

    Removal is by position, not by value: a finding titled "Claude findings" would otherwise
    lose its own ``--title`` argument.
    """
    out = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--project" and i + 1 < len(argv):
            skip = True
            continue
        out.append(arg)
    return out


def is_project_failure(stderr: str | None) -> bool:
    """Whether ``gh``'s stderr blames the Project rather than the issue.

    A missing board reads "could not resolve to a ProjectV2"; a token without the `project`
    scope reads "missing required scopes". Both mean the issue itself is fine.
    """
    low = (stderr or "").lower()
    return "project" in low or "scope" in low
