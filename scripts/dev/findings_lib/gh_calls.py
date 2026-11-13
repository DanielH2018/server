"""The gh reads and writes `findings.py` makes, and the one place a plan is executed.

Everything here takes a `FindingsTools`, so a test answers gh from a fake rather than from
the network. `load_issues` defaults its own, which is how `load_backlog_issues` calls it
with nothing to inject.

`run` carries almost all of the write surface: it prints a plan under `--dry-run` and calls
`tools.gh` otherwise, so a command that goes through it does not have to remember which mode
it is in. `_create` is the exception, because it reads the created issue's URL back out of
gh's stdout and a printed plan has no URL to read. It calls `tools.gh` unconditionally, so a
dry run must be stopped BEFORE it — `cmd_open` does that, branching on `args.dry_run` and
calling `run` instead.
"""

import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import (
    _LIST_FIELDS,
    comment_cap_warning,
    find_by_fingerprint,
    pr_refs,
    waited_on,
)
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
# fingerprint, `history` and `show` ask it for one topic or one issue, and the backlog cron
# reads the three narrow slices `load_backlog_issues` names. The largest of those is 49
# issues.
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
    search: str = "",
) -> list[dict]:
    """Fetches every ``claude``-labeled issue from gh, warning when it hits the list cap.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
        tools: the boundaries to reach gh through; the real ones when omitted, which is how
            `load_backlog_issues` calls it.
        labels: extra labels to narrow by. `gh issue list` ANDs repeated `--label`, so each
            one here is a further filter on the `claude` register, never a union.
        search: a GitHub search query ANDed with the filters above, for what no flag
            expresses, such as a close's state reason.
    """
    argv = ["issue", "list", "--label", "claude"]
    for label in labels:
        argv += ["--label", label]
    if search:
        argv += ["--search", search]
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
            "cap -- the register past it is missing, not empty; `findings.py history` "
            "searches past findings without the cap\n"
        )
    return _warn_at_the_comment_cap(issues)


# The labels `findings.py close` writes on its two not-planned outcomes, and the whole of
# what the settled register renders. `NO_REOPEN` is the same pair read for the other half of
# the contract — `open` refusing to re-file either.
SETTLED_LABELS = ("refuted", "accepted")

# A not-planned close that carries neither settled label: closed by hand, outside
# `findings.py close`, so no ruling and no reason were recorded.
UNRULED_CLOSE_SEARCH = "reason:not-planned " + " ".join(
    f"-label:{label}" for label in SETTLED_LABELS
)


def load_backlog_issues(tools: FindingsTools | None = None) -> list[dict]:
    """The rows `scripts/docs/reference/backlog.py` renders, in four narrow fetches.

    NOT `--state all`. `backlog.render_markdown` renders exactly four sets — the open
    findings, the closed `refuted` ones, the closed `accepted` ones and the not-planned
    closes carrying neither label — and drops every other closed issue on the floor.
    Fetching the whole register to throw most of it away costs 58.4s for 995 issues and
    grows about 350 issues a week, which would break `REGISTER_FETCH_TIMEOUT` above at about
    5,100 issues with the backlog page silently stale behind it. The first three sets were
    49 + 26 + 21 = 96 issues, the fourth was 21 on 2026-11-13, and they grow with what is
    FILED and SETTLED rather than with what is closed, so this cost does not track
    the register's size.

    Returns them deduplicated by issue number: an issue carrying both settled labels comes
    back from two of the fetches and must render once.
    """
    tools = tools or FindingsTools()
    fetched = load_issues("open", tools)
    for label in SETTLED_LABELS:
        fetched += load_issues("closed", tools, labels=(label,))
    fetched += load_issues("closed", tools, search=UNRULED_CLOSE_SEARCH)
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


# What `history` and `show` read: the register's fields plus the PRs that closed an issue.
# Kept apart from `_LIST_FIELDS`, which the claim fold and the backlog cron share and which
# does not need it.
HISTORY_FIELDS = f"{_LIST_FIELDS},closedByPullRequestsReferences"


def search_register(query: str, tools: FindingsTools, *, limit: int) -> list[dict]:
    """The `claude` issues in any state that gh's search index matches ``query`` against.

    Measured 2026-10-09: a topic search returning two issues took 0.6-0.9s, where the
    `--state all` register read it replaces took 58.4s and stopped at 1000 issues. The
    cost is the search index's lag — an issue filed seconds ago may not match yet, which
    `fingerprint_match`'s DECIDED marker weighs for `open`.
    """
    return as_object_list(
        tools.gh_json(
            "issue",
            "list",
            "--label",
            "claude",
            "--state",
            "all",
            "--limit",
            str(limit),
            "--search",
            query,
            "--json",
            HISTORY_FIELDS,
        ),
        "gh issue list --search",
    )


def open_pr_refs(tools: FindingsTools) -> set[int]:
    """Issue numbers the open PRs say they close, for `next` to withhold."""
    prs = as_object_list(
        tools.gh_json(
            "pr", "list", "--state", "open", "--limit", "200", "--json", "body"
        ),
        "gh pr list",
    )
    return pr_refs([str(pr.get("body") or "") for pr in prs])


# Each slice code costs one search, and a body that names many is citing a plan, not a wait.
SLICE_LOOKUP_CAP = 5


def blocker_hints(
    body: str, own: int | str, blocked_by: list[str], tools: FindingsTools
) -> list[str]:
    """Hint lines for each issue ``body`` says the finding waits on but is not blocked by.

    `issue_model.waited_on` finds the references; this resolves each one. A slice code
    resolves to the issue whose title starts `<code>:`, the way slices are filed, and a code
    with no such issue is skipped. Reads only, and only when the body names a wait.

    Args:
        own: the finding's number, or a placeholder before it exists.
        blocked_by: the blockers already recorded, which need no hint.
    """
    numbers, codes = waited_on(body)
    skip = {str(own), *(b.lstrip("#") for b in blocked_by)}
    found: dict[int, tuple[str, str]] = {}
    for n in numbers:
        if str(n) in skip:
            continue
        issue = as_object(
            tools.gh_json("issue", "view", str(n), "--json", "number,state"),
            "gh issue view",
        )
        found.setdefault(n, (f"#{n}", str(issue.get("state"))))
    for code in codes[:SLICE_LOOKUP_CAP]:
        hits = as_object_list(
            tools.gh_json(
                "issue", "list", "--state", "all", "--limit", "5",
                "--search", f'"{code}:" in:title', "--json", "number,title,state",
            ),
            "gh issue list --search",
        )  # fmt: skip
        for hit in hits:
            n = int(str(hit["number"]))
            if str(hit.get("title", "")).startswith(f"{code}:"):
                if str(n) not in skip:
                    found.setdefault(n, (f"{code} (#{n})", str(hit.get("state"))))
                break
    lines = []
    for n, (label, state) in found.items():
        if state == "OPEN":
            lines.append(
                f"hint: the body waits on {label}, which is open — `findings.py block "
                f"{own} --on {n}` withholds it until that closes, where a date only guesses"
            )
        else:
            lines.append(
                f"hint: the body waits on {label}, which is already closed — check "
                "whether the wait is over"
            )
    return lines


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


def _create(argv: list[str], tools: FindingsTools) -> str:
    """Runs an `issue create` argv and returns the created issue's URL."""
    return tools.gh(*_aimed_argv(argv, tools)).stdout.strip()
