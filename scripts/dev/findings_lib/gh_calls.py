"""The gh reads and writes `findings.py` makes, and the one place a plan is executed.

Everything here takes a `FindingsTools`, so a test answers gh from a fake rather than from
the network. `load_issues` defaults its own, which is how `scripts/docs/reference/backlog.py`
calls it with nothing to inject.

`run` carries almost all of the write surface: it prints a plan under `--dry-run` and calls
`tools.gh` otherwise, so a command that goes through it does not have to remember which mode
it is in. `_create_with_optional_project` is the exception, because it reads the created
issue's URL back out of gh's stdout and a printed plan has no URL to read. It calls
`tools.gh` unconditionally, so a dry run must be stopped BEFORE it — `cmd_open` does that at
`findings.py:130`, branching on `args.dry_run` and calling `run` instead.
"""

import subprocess
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import (
    _LIST_FIELDS,
    PROJECT_TITLE,
    comment_cap_warning,
    pr_refs,
)
from dev.findings_lib.plans import is_project_failure, without_project
from dev.findings_lib.boundaries import FindingsTools


def _warn_at_the_comment_cap(issues: list[dict]) -> list[dict]:
    """Passes ``issues`` through, warning on stderr about any at gh's comment page cap.

    THE FOLD GOES BLIND PAST THE CAP (#1284). gh asks for `comments(first: 100)` and nothing
    paginates, so a release comment past the cap would leave an issue claimed forever and a
    claim past it would make the read-back find nothing. The read itself is unchanged —
    paginating means leaving `gh issue list --json` for the REST API on every command, for a
    case no issue in the register is near — so a claim verdict that may be wrong announces
    itself instead of being silently wrong.
    """
    for issue in issues:
        warning = comment_cap_warning(issue)
        if warning:
            sys.stderr.write(warning + "\n")
    return issues


def load_issues(state: str = "all", tools: FindingsTools | None = None) -> list[dict]:
    """Fetches every ``claude``-labeled issue from gh, up to 1000.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
        tools: the boundaries to reach gh through; the real ones when omitted, which is how
            `scripts/docs/reference/backlog.py` calls it.
    """
    argv = ("issue", "list", "--label", "claude", "--state", state, "--limit", "1000")
    issues = (tools or FindingsTools()).gh_json(*argv, "--json", _LIST_FIELDS) or []
    return _warn_at_the_comment_cap(issues)


def open_pr_refs(tools: FindingsTools) -> set[int]:
    """Issue numbers the open PRs say they close, for `next` to withhold."""
    prs = tools.gh_json(
        "pr", "list", "--state", "open", "--limit", "200", "--json", "body"
    )
    return pr_refs([pr.get("body") or "" for pr in prs or []])


def _existing_labels(tools: FindingsTools) -> set[str]:
    labels = tools.gh_json("label", "list", "--limit", "200", "--json", "name")
    return {lab["name"] for lab in labels or []}


def _load_issue(number: int, tools: FindingsTools) -> dict:
    issue = tools.gh_json("issue", "view", str(number), "--json", _LIST_FIELDS)
    _warn_at_the_comment_cap([issue] if issue else [])
    return issue


def run(plans: list[list[str]], dry_run: bool, tools: FindingsTools) -> None:
    for argv in plans:
        if dry_run:
            print("gh " + " ".join(argv))
        else:
            tools.gh(*argv)


def _create_with_optional_project(argv: list[str], tools: FindingsTools) -> str:
    """Run the create argv, retrying without ``--project`` if the board is the only problem.

    Returns the created issue's URL. The board is a view; losing it must not lose the
    finding, so a Project failure warns and the issue is created anyway.
    """
    try:
        return tools.gh(*argv).stdout.strip()
    except subprocess.CalledProcessError as exc:
        if not is_project_failure(exc.stderr):
            raise
        first_line = (exc.stderr or "").strip().partition("\n")[0]
        url = tools.gh(*without_project(argv)).stdout.strip()
        sys.stderr.write(
            f'warning: not added to Project "{PROJECT_TITLE}": {first_line}\n'
        )
        return url
