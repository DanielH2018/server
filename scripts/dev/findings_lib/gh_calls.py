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
    find_by_fingerprint,
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


# `gh issue list` returns at most this many and says nothing when it truncates. The
# settled register in docs/reference/backlog.md reads `state="all"`, which stood at 645 of
# these on 2026-09-21 and 995 on 2026-09-28, so a silent cut there would drop refuted
# findings from the table a reviewer reads before flagging -- the harm the register exists
# to prevent. Raised from 1000 five issues before the register reached it. `gh` itself
# pages past 1000: `gh issue list --state all --limit 1005 --json number` on this repo
# returned 1001 issues on 2026-09-28, so the cap is ours alone and the warning below still
# means what it says. The remaining `--state all` caller is the backlog cron
# (`scripts/docs/reference/backlog.py`), which can afford the minute this costs; `open`
# stopped reading the whole register in #2846.
ISSUE_LIST_CAP = 5000

# `lib.gh.gh`'s default timeout is 60s, which the `--state all` fetch outgrew. It asks for
# `body` and `comments` on every `claude` issue in every state, and on 2026-09-28 that was
# 4.19 MB across ~900 issues and took 67.5s wall — so EVERY findings.py subcommand failed,
# `open` included, with a bare "gh failed: ... timed out after 60.0 seconds" (#2800's session
# could not file its own follow-ups). The cost grows with the register and nothing else here
# does, so the timeout is set at this call site rather than raised for every `gh` caller.
#
# #2846 proposed narrowing `_LIST_FIELDS` per subcommand as the durable fix. MEASURED ON
# 2026-09-28, THAT IS THE WRONG LEVER, so do not reach for it: the field set is not what
# makes this slow, the STATE is. `--state all` was 995 issues and 58.4s; `--state open` was
# 57 issues and 0.9s. Every subcommand but `open` already reads `open` only, and the fields
# they would drop are the ones `issue_model.issue_rows` structurally needs — `body` for
# `verify_by` and `paths`, `comments` for `claimed` and `reobservations`. What #2846 fixed
# instead is `open`: it asks gh to find the one fingerprint rather than fetching the
# register (`fingerprint_match` below). This timeout now covers the backlog cron alone.
REGISTER_FETCH_TIMEOUT = 300.0


def load_issues(state: str = "all", tools: FindingsTools | None = None) -> list[dict]:
    """Fetches every ``claude``-labeled issue from gh, warning when it hits the list cap.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
        tools: the boundaries to reach gh through; the real ones when omitted, which is how
            `scripts/docs/reference/backlog.py` calls it.
    """
    argv = ("issue", "list", "--label", "claude", "--state", state, "--limit")
    issues = (tools or FindingsTools()).gh_json(
        *argv,
        str(ISSUE_LIST_CAP),
        "--json",
        _LIST_FIELDS,
        timeout=REGISTER_FETCH_TIMEOUT,
    ) or []
    if len(issues) >= ISSUE_LIST_CAP:
        sys.stderr.write(
            f"warning: gh returned {ISSUE_LIST_CAP} issues for --state {state}, its list "
            "cap -- the register past it is missing, not empty\n"
        )
    return _warn_at_the_comment_cap(issues)


# How many search hits `fingerprint_match` reads back. A fingerprint is a 12-hex string that
# appears once, in the body of the one issue that owns it, so a second hit is a comment or a
# PR body quoting it. 30 leaves room for a well-discussed finding without paying for the
# register.
FINGERPRINT_SEARCH_LIMIT = 30


# DECIDED: `open` asks gh's search index for the one fingerprint instead of fetching the
# register and scanning it. Measured 2026-09-28 on daniel-server: the whole-register read
# this replaced was 995 issues, 4.3 MB and 58.4s, and it grew with every finding filed —
# #2800 had already watched it blow through `lib.gh.gh`'s 60s default and take EVERY
# findings.py subcommand down with it. The search is 0.6s and does not grow. The cost is
# gh's search INDEX LAG: an issue filed seconds ago may not be findable yet, so two sessions
# filing one fingerprint inside that window both create. That trade is right — a duplicate
# issue is one `close --refuted` away, while a filer that cannot run drops the finding into
# a reply and loses it, which is the harm the register exists to prevent. Long form: #2846.
def fingerprint_match(fp: str, tools: FindingsTools) -> dict | None:
    """The register's issue carrying fingerprint ``fp``, in any state, or None.

    Search NARROWS, it never decides. The hits go through the same
    `issue_model.find_by_fingerprint` the register scan used, which reads the body trailer
    only — so a comment or a PR body quoting a fingerprint is dropped here rather than
    dedup-matching an unrelated issue.
    """
    hits = (
        tools.gh_json(
            "issue",
            "list",
            "--label",
            "claude",
            "--state",
            "all",
            "--limit",
            str(FINGERPRINT_SEARCH_LIMIT),
            "--search",
            fp,
            "--json",
            _LIST_FIELDS,
        )
        or []
    )
    found = find_by_fingerprint(_warn_at_the_comment_cap(hits), fp)
    if found is None and len(hits) >= FINGERPRINT_SEARCH_LIMIT:
        # The one way this returns a wrong None: the owning issue was pushed out of the page
        # by issues that merely mention the fingerprint. Says so rather than filing a silent
        # duplicate, because the caller's next act is to create one.
        sys.stderr.write(
            f"warning: {FINGERPRINT_SEARCH_LIMIT} search hits for fingerprint {fp} and none "
            "owns it -- the owning issue may be past the page, so check before filing\n"
        )
    return found


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
