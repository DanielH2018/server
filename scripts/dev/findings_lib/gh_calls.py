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

import json
from collections.abc import Mapping
from dataclasses import replace
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import (
    _LIST_FIELDS,
    PR_REPO,
    REGISTER_AUTHOR,
    Blocker,
    Comment,
    Issue,
    IssueState,
    comment_cap_warning,
    find_by_fingerprint,
    parse_issue,
    pr_refs,
    waited_on,
)
from dev.findings_lib.boundaries import FindingsTools
from lib.json_types import JsonObject, as_object, as_object_list


def _warn_at_the_comment_cap(issues: list[Issue]) -> list[Issue]:
    """Passes ``issues`` through, warning on stderr about any at gh's comment page cap.

    THE FOLD GOES BLIND PAST THE CAP. gh asks for `comments(first: 100)` and nothing
    paginates, so a release comment past the cap would leave an issue claimed forever and a
    claim past it would make the read-back find nothing. The read itself is unchanged —
    paginating means leaving `gh issue list --json` for the REST API on every command, for a
    case no issue in the register is near — so a claim verdict that may be wrong announces
    itself instead of being silently wrong.
    """
    for issue in issues:
        warning = comment_cap_warning(issue.number, len(issue.comments))
        if warning:
            sys.stderr.write(warning + "\n")
    return issues


# An aliased `issue(number:)` lookup of the field values alone: one request and one point for
# the 113 open issues, 0.79s, measured 2026-11-19. `gh issue list --json` cannot return them.
_FIELDS_PER_CALL = 100
_FIELD_VALUES = """issueFieldValues(first: 25) { nodes {
      ... on IssueFieldSingleSelectValue { name field { ... on IssueFieldSingleSelect { name } } }
      ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } } } }"""


def _graphql(tools: FindingsTools, query: str) -> JsonObject:
    owner, name = (tools.repo or PR_REPO).split("/", 1)
    data = as_object(
        tools.gh_json(
            "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}",
            "-F", f"name={name}", timeout=REST_TIMEOUT,
        ),
        "gh api graphql",
    )  # fmt: skip
    return as_object(data.get("data"), "gh api graphql data")


def _nodes(parent: JsonObject, key: str) -> list[JsonObject]:
    return as_object_list(
        as_object(parent.get(key) or {}, key).get("nodes"), f"{key}.nodes"
    )


def _field_map(issue: JsonObject) -> dict[str, str]:
    out = {}
    for value in _nodes(issue, "issueFieldValues"):
        name = as_object(value.get("field") or {}, "field").get("name")
        if isinstance(name, str):
            out[name] = str(value.get("name") or value.get("value"))
    return out


def attach_fields(issues: list[Issue], tools: FindingsTools) -> list[Issue]:
    """``issues``, each with `fields` holding its issue-field values by field name.

    Every loader calls this, because `Issue.not_before` and `Issue.severity` read `fields`,
    and an issue missing them reads as undeferred. So a failed read raises rather than
    returning issues with no fields: a command that cannot see the dates must not offer
    deferred issues.
    """
    numbers = [i.number for i in issues]
    by_number: dict[int, dict[str, str]] = {}
    for start in range(0, len(numbers), _FIELDS_PER_CALL):
        chunk = numbers[start : start + _FIELDS_PER_CALL]
        lookups = "\n".join(
            f"  i{n}: issue(number: {n}) {{ {_FIELD_VALUES} }}" for n in chunk
        )
        query = (
            "query($owner: String!, $name: String!) {\n"
            f" repository(owner: $owner, name: $name) {{\n{lookups}\n }}\n}}"
        )
        repository = as_object(_graphql(tools, query).get("repository"), "repository")
        for n in chunk:
            by_number[n] = _field_map(as_object(repository.get(f"i{n}") or {}, f"#{n}"))
    return [replace(i, fields=by_number.get(i.number, {})) for i in issues]


def set_fields(
    number: int | str,
    values: Mapping[str, str | None],
    tools: FindingsTools,
    dry_run: bool,
    *,
    issue_type: str | None = None,
) -> None:
    """Sets each issue field in ``values``, and ``issue_type`` if given, on issue ``number``.

    A None value clears that field. ``number`` is a string only under ``dry_run``, where
    `open` names an issue not yet made.

    A single-select value is the option's name, and ``issue_type`` is the type's name. Raises
    LookupError naming a field, an option or a type the account lacks, so the caller fails
    rather than silently dropping the value. Both writes go in one mutation document.
    """
    if dry_run:
        for name, value in values.items():
            print(
                f"gh api graphql setIssueFieldValue #{number} {name}={value or '<clear>'}"
            )
        if issue_type:
            print(f"gh api graphql updateIssue #{number} issueType={issue_type}")
        return
    query = (
        "query($owner: String!, $name: String!) {\n"
        " repository(owner: $owner, name: $name) {\n"
        f"  issue(number: {number}) {{ id }}\n"
        "  issueTypes(first: 30) { nodes { id name } }\n"
        "  issueFields(first: 30) { nodes {\n"
        "   ... on IssueFieldSingleSelect { id name options { id name } }\n"
        "   ... on IssueFieldDate { id name } } }\n }\n}"
    )
    repository = as_object(_graphql(tools, query).get("repository"), "repository")
    issue_id = json.dumps(as_object(repository.get("issue"), f"issue #{number}")["id"])
    defs = {f.get("name"): f for f in _nodes(repository, "issueFields")}
    entries = []
    for name, value in values.items():
        field = defs.get(name)
        if field is None:
            raise LookupError(
                f'no "{name}" issue field; add it under Settings > Planning > Issue fields'
            )
        if value is None:
            entries.append(f"{{fieldId: {json.dumps(field['id'])}, delete: true}}")
        elif "options" in field:
            options = as_object_list(field["options"], f"{name} options")
            option = next((o for o in options if o.get("name") == value), None)
            if option is None:
                raise LookupError(f'issue field "{name}" has no option "{value}"')
            entries.append(
                f"{{fieldId: {json.dumps(field['id'])}, "
                f"singleSelectOptionId: {json.dumps(option['id'])}}}"
            )
        else:
            entries.append(
                f"{{fieldId: {json.dumps(field['id'])}, dateValue: {json.dumps(value)}}}"
            )
    writes = []
    if entries:
        writes.append(
            f"setIssueFieldValue(input: {{issueId: {issue_id}, "
            f"issueFields: [{', '.join(entries)}]}}) {{ issue {{ id }} }}"
        )
    if issue_type:
        types = {t.get("name"): t["id"] for t in _nodes(repository, "issueTypes")}
        if issue_type not in types:
            raise LookupError(
                f'no "{issue_type}" issue type; add it under Settings > Planning > Issue types'
            )
        writes.append(
            f"updateIssue(input: {{id: {issue_id}, "
            f"issueTypeId: {json.dumps(types[issue_type])}}}) {{ issue {{ id }} }}"
        )
    if writes:
        mutation = (
            "mutation { " + " ".join(f"w{i}: {w}" for i, w in enumerate(writes)) + " }"
        )
        tools.gh_json("api", "graphql", "-f", f"query={mutation}", timeout=REST_TIMEOUT)


def fill_missing(
    issue: Issue,
    values: dict[str, str],
    issue_type: str,
    tools: FindingsTools,
    dry_run: bool,
) -> None:
    """Sets each of ``values`` and ``issue_type`` that ``issue`` does not hold yet.

    The repair for an `open` whose second write failed after `gh issue create`: re-running
    the same `open` fills only what that write left unset, and never overwrites a value
    set since.
    """
    missing = {name: v for name, v in values.items() if not issue.fields.get(name)}
    held_type = issue.issue_type
    if missing or not held_type:
        set_fields(
            issue.number,
            missing,
            tools,
            dry_run,
            issue_type=None if held_type else issue_type,
        )


def _operator_issues(issues: list[Issue]) -> list[Issue]:
    """The issues in ``issues`` that ``REGISTER_AUTHOR`` opened, warning about the rest."""
    kept = [i for i in issues if i.author == REGISTER_AUTHOR]
    dropped = [i for i in issues if i.author != REGISTER_AUTHOR]
    if dropped:
        listed = ", ".join(f"#{i.number} by {i.author or '?'}" for i in dropped)
        sys.stderr.write(
            f"warning: skipped {len(dropped)} issue(s) not opened by {REGISTER_AUTHOR}: "
            f"{listed}\n"
        )
    return kept


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
# No caller fetches the whole register, so none is near this: every `findings.py`
# subcommand reads `--state open`, `open` asks gh's search index for one fingerprint,
# `history` and `show` ask it for one topic or one issue, and the backlog cron reads the three
# narrow slices `load_backlog_issues` names.
ISSUE_LIST_CAP = 1000

# `lib.gh.gh`'s default timeout is 60s, which a whole-register `--state all` fetch outgrows:
# asking for `body` and `comments` on every `claude` issue in every state takes about 58s per
# 1000 issues, so EVERY findings.py subcommand would fail, `open` included, with a bare "gh
# failed: ... timed out after 60.0 seconds". Raising the timeout only moves the wall (300s
# breaks at about 5,100 issues), and a fetch that times out never reaches the cap warning that
# would have explained it.
#
# NARROWING THE FIELD SET IS THE WRONG LEVER — the field set is not what makes a fetch slow,
# the STATE is (`--state all` takes about a minute, `--state open` under a second), and
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
) -> list[Issue]:
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
    raw = as_object_list(
        (tools or FindingsTools()).gh_json(
            *argv,
            str(ISSUE_LIST_CAP),
            "--json",
            _LIST_FIELDS,
            timeout=REGISTER_FETCH_TIMEOUT,
        ),
        "gh issue list",
    )
    issues = [parse_issue(i) for i in raw]
    if len(issues) >= ISSUE_LIST_CAP:
        sys.stderr.write(
            f"warning: gh returned {ISSUE_LIST_CAP} issues for --state {state}, its list "
            "cap -- the register past it is missing, not empty; `findings.py history` "
            "searches past findings without the cap\n"
        )
    return attach_fields(
        _warn_at_the_comment_cap(_operator_issues(issues)), tools or FindingsTools()
    )


# The labels `findings.py close` writes on its two not-planned outcomes, and the whole of
# what the settled register renders. `NO_REOPEN` is the same pair read for the other half of
# the contract — `open` refusing to re-file either.
SETTLED_LABELS = ("refuted", "accepted")

# A not-planned close that carries neither settled label: closed by hand, outside
# `findings.py close`, so no ruling was recorded.
UNRULED_CLOSE_SEARCH = "reason:not-planned " + " ".join(
    f"-label:{label}" for label in SETTLED_LABELS
)


def load_backlog_issues(tools: FindingsTools | None = None) -> list[Issue]:
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
    by_number: dict[int, Issue] = {}
    for issue in fetched:
        by_number.setdefault(issue.number, issue)
    return list(by_number.values())


# How many search hits `fingerprint_match` reads back. A fingerprint is a 12-hex string that
# appears once, in the body of the one issue that owns it, so a second hit is a comment or a
# PR body quoting it. 30 leaves room for a well-discussed finding without paying for the
# register.
FINGERPRINT_SEARCH_LIMIT = 30


# DECIDED: `open` asks gh's search index for the one fingerprint instead of fetching the
# register and scanning it. A whole-register read grows with every finding filed and
# would blow through `lib.gh.gh`'s 60s default and take EVERY findings.py subcommand down
# with it. The search is fast and does not grow. The cost is
# gh's search INDEX LAG: an issue filed seconds ago may not be findable yet, so two sessions
# filing one fingerprint inside that window both create. That trade is right — a duplicate
# issue is one `close --refuted` away, while a filer that cannot run drops the finding into
# a reply and loses it, which is the harm the register exists to prevent.
def fingerprint_match(fp: str, tools: FindingsTools) -> Issue | None:
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
    found = find_by_fingerprint(
        _warn_at_the_comment_cap([parse_issue(h) for h in hits]), fp
    )
    if found is not None:
        return attach_fields([found], tools)[0]
    if len(hits) >= FINGERPRINT_SEARCH_LIMIT:
        # The one way this returns a wrong None: the owning issue was pushed out of the page
        # by issues that merely mention the fingerprint. Says so rather than filing a silent
        # duplicate, because the caller's next act is to create one.
        sys.stderr.write(
            f"warning: {FINGERPRINT_SEARCH_LIMIT} search hits for fingerprint {fp} and none "
            "owns it -- the owning issue may be past the page, so check before filing\n"
        )
    return None


# What `history` and `show` read: the register's fields plus the PRs that closed an issue.
# Kept apart from `_LIST_FIELDS`, which the claim fold and the backlog cron share and which
# does not need it.
HISTORY_FIELDS = f"{_LIST_FIELDS},closedByPullRequestsReferences"


def search_register(query: str, tools: FindingsTools, *, limit: int) -> list[Issue]:
    """The `claude` issues in any state that gh's search index matches ``query`` against.

    Measured 2026-10-09: a topic search returning two issues took 0.6-0.9s, where the
    `--state all` register read it replaces took 58.4s and stopped at 1000 issues. The
    cost is the search index's lag — an issue filed seconds ago may not match yet, which
    `fingerprint_match`'s DECIDED marker weighs for `open`.
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
            str(limit),
            "--search",
            query,
            "--json",
            HISTORY_FIELDS,
        ),
        "gh issue list --search",
    )
    return attach_fields([parse_issue(h) for h in hits], tools)


def open_pr_refs(tools: FindingsTools) -> set[int]:
    """Issue numbers the open PRs say they close, for `next` to withhold."""
    prs = as_object_list(
        tools.gh_json(
            "pr", "list", "--state", "open", "--limit", "200", "--json", "body"
        ),
        "gh pr list",
    )
    return pr_refs([str(pr.get("body") or "") for pr in prs])


# The REST reads below serve the `findings` cc-wait source, which polls for hours. `gh issue
# list` and `gh pr list` spend the GraphQL quota, the one sessions exhaust
# (`docs/github-quota.md`); REST is a separate 5,000-request bucket. A whole read must fit
# inside cc-wait's 60s per-read limit, hence the short per-call timeout.
REST_TIMEOUT = 20.0


def _rest_list(tools: FindingsTools, path: str) -> list[JsonObject]:
    """``GET repos/<register>/<path>`` as a list of objects; `aimed` adds no `--repo` to it."""
    url = f"repos/{tools.repo or PR_REPO}/{path}"
    return as_object_list(tools.gh_json("api", url, timeout=REST_TIMEOUT), url)


def newest_open_issues(tools: FindingsTools) -> list[JsonObject]:
    """The 100 newest open `claude` issues, newest first, in REST's own shape.

    One request. Issue numbers rise with creation time, so the first entry is the highest
    open number. REST's issue list includes pull requests, which are dropped here.
    """
    raw = _rest_list(
        tools,
        "issues?labels=claude&state=open&sort=created&direction=desc&per_page=100",
    )
    return [i for i in raw if "pull_request" not in i]


def _rest_state(data: JsonObject) -> IssueState:
    """REST's lower-case `state` as an `IssueState`; KeyError when it is missing."""
    return IssueState(str(data["state"]).upper())


def _login(data: JsonObject) -> str:
    return str(as_object(data.get("user") or {}, "user").get("login") or "")


def _rest_comment(comment: JsonObject) -> Comment:
    return Comment(
        body=str(comment.get("body") or ""),
        author=_login(comment),
        association=str(comment.get("author_association") or ""),
        created_at=str(comment.get("created_at") or ""),
    )


def rest_issue(data: JsonObject) -> Issue:
    """One REST issue as an `Issue`, without the comments and blockers it costs a read to get.

    Raises:
        KeyError: the issue carries no `number` or `state`.
    """
    issue_type = as_object(data.get("type") or {}, "type").get("name")
    values = as_object_list(data.get("issue_field_values") or [], "issue_field_values")
    milestone = data.get("milestone")
    return Issue(
        number=int(str(data["number"])),
        title=str(data.get("title") or ""),
        state=_rest_state(data),
        body=str(data.get("body") or ""),
        labels=frozenset(
            str(lab["name"])
            for lab in as_object_list(data.get("labels") or [], "labels")
        ),
        created_at=str(data.get("created_at") or ""),
        url=str(data.get("html_url") or ""),
        author=_login(data),
        issue_type=str(issue_type) if issue_type else None,
        milestone=as_object(milestone, "milestone") if milestone else None,
        fields={
            str(v["issue_field_name"]): str(
                as_object(v.get("single_select_option") or {}, "option").get("name")
                or v.get("value")
            )
            for v in values
        },
    )


def threaded_issues(raw: list[JsonObject], tools: FindingsTools) -> list[Issue]:
    """``raw`` REST issues as `Issue`s, comments and blockers included.

    Each issue costs one more request per thread it has: its comments when it has any, and
    its blockers when it has an open one. Issues another account opened are dropped with
    `load_issues`'s warning.

    Raises:
        KeyError: an issue or a blocker carries no `state`.
    """
    issues = []
    for data in raw:
        issue = rest_issue(data)
        comments = (
            _rest_list(tools, f"issues/{issue.number}/comments?per_page=100")
            if data.get("comments")
            else []
        )
        summary = as_object(
            data.get("issue_dependencies_summary") or {}, "dependencies"
        )
        blockers = (
            _rest_list(tools, f"issues/{issue.number}/dependencies/blocked_by")
            if summary.get("blocked_by")
            else []
        )
        issues.append(
            replace(
                issue,
                comments=tuple(_rest_comment(c) for c in comments),
                blockers=tuple(
                    Blocker(
                        int(str(b["number"])),
                        _rest_state(b),
                        str(b.get("html_url") or ""),
                    )
                    for b in blockers
                ),
            )
        )
    return _operator_issues(issues)


def rest_open_pr_refs(tools: FindingsTools) -> set[int]:
    """`open_pr_refs` read through REST, in one request."""
    prs = _rest_list(tools, "pulls?state=open&per_page=100")
    return pr_refs([str(pr.get("body") or "") for pr in prs])


# Each slice code costs one search, and a body that names many is citing a plan, not a wait.
SLICE_LOOKUP_CAP = 5


def blocker_hints(
    body: str, own: int | str, blocked_by: list[str], tools: FindingsTools
) -> list[str]:
    """Hint lines for each issue ``body`` says the finding waits on but is not blocked by.

    `issue_model.waited_on` finds the references; this resolves each one. A slice code
    resolves to the issue whose title starts `<code>:`, the way slices are filed, and a code
    with no such issue is skipped. A pull request is skipped too: `gh issue view` answers
    for one, with a `/pull/` url, but GitHub's blocked-by relation takes issues only. Reads
    only, and only when the body names a wait.

    Args:
        own: the finding's number, or a placeholder before it exists.
        blocked_by: the blockers already recorded, which need no hint.
    """
    numbers, codes = waited_on(body)
    skip = {str(own), *(b.lstrip("#") for b in blocked_by)}
    found: dict[int, tuple[str, IssueState]] = {}
    for n in numbers:
        if str(n) in skip:
            continue
        issue = as_object(
            tools.gh_json("issue", "view", str(n), "--json", "number,state,url"),
            "gh issue view",
        )
        if "/pull/" in str(issue.get("url", "")):
            continue
        found.setdefault(n, (f"#{n}", IssueState(issue["state"])))
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
                    found.setdefault(n, (f"{code} (#{n})", IssueState(hit["state"])))
                break
    lines = []
    for n, (label, state) in found.items():
        if state == IssueState.OPEN:
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


def _load_issue(number: int, tools: FindingsTools) -> Issue:
    issue = parse_issue(
        as_object(
            tools.gh_json("issue", "view", str(number), "--json", _LIST_FIELDS),
            "gh issue view",
        )
    )
    if issue.author != REGISTER_AUTHOR:
        raise SystemExit(
            f"#{number} was opened by {issue.author or 'an unknown account'}, not "
            f"{REGISTER_AUTHOR}, so findings.py does not read it"
        )
    _warn_at_the_comment_cap([issue])
    return attach_fields([issue], tools)[0]


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


def created_number(url: str) -> int | None:
    """The issue number at the end of the URL `gh issue create` prints, or None."""
    tail = url.strip().rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
