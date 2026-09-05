"""What `findings.py` decides to do, as gh argv nobody has run yet.

Every command in `findings.py` plans first and runs second, so the decision — file, touch,
reopen, refuse, escalate, close — is a pure function of the issue it was handed. `--dry-run`
prints what these return; `findings_gh.run` is the only thing that executes them.

`is_project_failure` and `without_project` are the exception that proves the shape: they are
also pure, and they let `findings_gh._create_with_optional_project` decide whether a `gh`
failure is worth retrying without ever looking at gh itself.
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.findings_model import (
    LABELS,
    PROJECT_TITLE,
    _REOBSERVED,
    label_names,
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
        ``reopened`` or ``refuted``; exit_code is 3 only for ``refuted``; plans is the gh
        argv list to run.
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
    if existing.get("state") == "CLOSED" and "refuted" in names:
        return "refuted", 3, []
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
    fixed: bool,
    pr: int | None,
    reason: str | None,
    comment: str | None = None,
) -> list[list[str]]:
    """Plans the gh argv to close an issue as fixed or refuted.

    Args:
        number: the issue number.
        fixed: True to close as completed, False to close as refuted.
        pr: the PR that fixed it, included in the close comment when given.
        reason: required when ``fixed`` is False; what disproved the finding.
        comment: overrides the default close comment (`verify` uses this to quote the
            verify-by command and its output instead of naming a PR).
    """
    n = str(number)
    if fixed:
        by = f" by PR #{pr}" if pr else ""
        text = comment or f"Fixed{by}."
        return [["issue", "close", n, "--reason", "completed", "--comment", text]]
    return [
        ["issue", "edit", n, "--add-label", "refuted"],
        [
            "issue",
            "close",
            n,
            "--reason",
            "not planned",
            "--comment",
            f"Refuted: {reason}",
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
