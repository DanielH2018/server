"""The gh reads and writes `findings.py` makes, and the one place a plan is executed.

Everything here takes a `FindingsTools`, so a test answers gh from a fake rather than from
the network. `load_issues` defaults its own, which is how `load_backlog_issues` calls it
with nothing to inject.

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
from lib.json_types import JsonObject, as_object, as_object_list


def _warn_at_the_comment_cap(issues: list[dict]) -> list[dict]:
    """Passes ``issues`` through, warning on stderr about any at gh's comment page cap.

    THE FOLD GOES BLIND PAST THE CAP. gh asks for `comments(first: 100)` and nothing
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


# The `--limit` every list here passes, and the number the warning below compares against.
# It is set to GH'S OWN CEILING ON A LABEL-FILTERED LIST on purpose. A `--label` sends `gh
# issue list` through GitHub's SEARCH API, which caps at 1000 whatever `--limit` says:
# `--label claude --state all` returns exactly 1000 even at `--limit 5000`. An UNFILTERED
# list pages past it; every list here carries `--label claude`, so 1000 is the number that
# binds.
#
# A cap above that ceiling can never make `len(issues) >= ISSUE_LIST_CAP` true, so the
# truncation the warning below exists to announce would be silent. At 1000 the warning
# fires exactly when gh truncates, which is what it says it does.
#
# Nothing fetches the whole register any more, so no live caller is near this: every
# `findings.py` subcommand reads `--state open`, `open` asks gh's search index for one
# fingerprint, and the backlog cron reads the three narrow slices `load_backlog_issues`
# names. The largest of those is 49 issues.
ISSUE_LIST_CAP = 1000

# `lib.gh.gh`'s default timeout is 60s, which a whole-register `--state all` fetch outgrows:
# asking for `body` and `comments` on every `claude` issue in every state is 4.19 MB across
# ~900 issues and takes 67.5s wall, so EVERY findings.py subcommand would fail, `open`
# included, with a bare "gh failed: ... timed out after 60.0 seconds". Raising the timeout
# only moved the wall: at ~58s per 1000 issues, 300s breaks at about 5,100 issues, and a
# fetch that times out never reaches the cap warning that would have explained it.
#
# NARROWING THE FIELD SET IS THE WRONG LEVER — the field set is not what makes a fetch slow,
# the STATE is (`--state all` 995 issues / 58.4s, `--state open` 57 issues / 0.9s), and
# `issue_model.issue_rows` structurally needs `body` for `verify_by` and `paths` and
# `comments` for `claimed` and `reobservations`. Narrowing the QUERY is the lever:
# `fingerprint_match` for `open`, `load_backlog_issues` for the cron. This timeout is a
# ceiling over fetches measured in seconds, kept because the register keeps growing and a
# slow LAN is not a reason to lose the page.
REGISTER_FETCH_TIMEOUT = 300.0


def load_issues(
    state: str = "all",
    tools: FindingsTools | None = None,
    labels: tuple[str, ...] = (),
) -> list[dict]:
    """Fetches every ``claude``-labeled issue from gh, warning when it hits the list cap.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
        tools: the boundaries to reach gh through; the real ones when omitted, which is how
            `load_backlog_issues` calls it.
        labels: extra labels to narrow by. `gh issue list` ANDs repeated `--label`, so each
            one here is a further filter on the `claude` register, never a union.
    """
    argv = ["issue", "list", "--label", "claude"]
    for label in labels:
        argv += ["--label", label]
    argv += ["--state", state, "--limit"]
    issues = as_object_list(
        (tools or FindingsTools()).gh_json(
            *argv,
            str(ISSUE_LIST_CAP),
            "--json",
            _LIST_FIELDS,
            timeout=REGISTER_FETCH_TIMEOUT,
        ),
        "gh issue list",
    )
    if len(issues) >= ISSUE_LIST_CAP:
        sys.stderr.write(
            f"warning: gh returned {ISSUE_LIST_CAP} issues for --state {state}, its list "
            "cap -- the register past it is missing, not empty\n"
        )
    return _warn_at_the_comment_cap(issues)


# The labels `findings.py close` writes on its two not-planned outcomes, and the whole of
# what the settled register renders. `NO_REOPEN` is the same pair read for the other half of
# the contract — `open` refusing to re-file either.
SETTLED_LABELS = ("refuted", "accepted")


def load_backlog_issues(tools: FindingsTools | None = None) -> list[dict]:
    """The rows `scripts/docs/reference/backlog.py` renders, in three narrow fetches.

    NOT `--state all`. `backlog.render_markdown` renders exactly three sets — the open
    findings, the closed `refuted` ones and the closed `accepted` ones — and drops every
    other closed issue on the floor. Fetching the whole register to throw most of it away
    costs 58.4s for 995 issues and grows about 350 issues a week, which would break
    `REGISTER_FETCH_TIMEOUT` above at about 5,100 issues with the backlog page silently
    stale behind it. The same three sets are 49 + 26 + 21 = 96 issues, and they grow with
    what is FILED and SETTLED rather than with what is closed, so this cost does not track
    the register's size.

    Returns them deduplicated by issue number: an issue carrying both settled labels comes
    back from two of the fetches and must render once.
    """
    tools = tools or FindingsTools()
    fetched = load_issues("open", tools)
    for label in SETTLED_LABELS:
        fetched += load_issues("closed", tools, labels=(label,))
    by_number: dict[int, dict] = {}
    for issue in fetched:
        by_number.setdefault(issue["number"], issue)
    return list(by_number.values())


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
    hits = as_object_list(
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
        ),
        "gh issue list --search",
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
    prs = as_object_list(
        tools.gh_json(
            "pr", "list", "--state", "open", "--limit", "200", "--json", "body"
        ),
        "gh pr list",
    )
    return pr_refs([str(pr.get("body") or "") for pr in prs])


def _existing_labels(tools: FindingsTools) -> set[str]:
    labels = as_object_list(
        tools.gh_json("label", "list", "--limit", "200", "--json", "name"),
        "gh label list",
    )
    return {str(lab["name"]) for lab in labels}


def _load_issue(number: int, tools: FindingsTools) -> JsonObject:
    issue = as_object(
        tools.gh_json("issue", "view", str(number), "--json", _LIST_FIELDS),
        "gh issue view",
    )
    _warn_at_the_comment_cap([issue] if issue else [])
    return issue


def _aimed_argv(argv: list[str], tools: FindingsTools) -> list[str]:
    """``argv`` with ``--repo`` appended when ``tools`` is aimed at another register.

    Appended after the subcommand pair: gh defines `--repo` on each subcommand, not on the
    root.
    """
    return argv if tools.repo is None else [*argv, "--repo", tools.repo]


def run(plans: list[list[str]], dry_run: bool, tools: FindingsTools) -> None:
    for argv in plans:
        argv = _aimed_argv(argv, tools)
        if dry_run:
            print("gh " + " ".join(argv))
        else:
            tools.gh(*argv)


def _create_with_optional_project(argv: list[str], tools: FindingsTools) -> str:
    """Run the create argv, retrying without ``--project`` if the board is the only problem.

    Returns the created issue's URL. The board is a view; losing it must not lose the
    finding, so a Project failure warns and the issue is created anyway.
    """
    argv = _aimed_argv(argv, tools)
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
