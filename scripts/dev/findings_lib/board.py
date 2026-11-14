"""The Server Issue Tracking board, kept equal to what each issue's state and labels say.

The board holds three fields findings.py owns, each derived from the issue alone:

    Status      Done when the issue is closed; In Progress when it carries `claimed` or an
                open pull request will close it; Todo otherwise.
    Severity    the `severity/<level>` label's level, or empty.
    Not before  the `not-before:<date>` label's date, or empty.

THE ISSUE STAYS THE RECORD. `next`, `claim` and the fan-out read labels and comments, never
the board, so a board write is best-effort: `findings.main` syncs every issue a command
wrote to, prints `board_warning`'s one line when that fails, and keeps the command's own
exit code. `findings.py sync-board` is the repair path.

Every call is `gh api graphql`. Issues are read in chunks of aliased lookups. A chunk's
writes go out as aliased mutations, a few to a request: the adds first, because the field
writes need their item ids.

Typical usage example:

    outcomes = sync_board([8237, 8240], repo=None)
"""

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import PR_REPO, not_before
from lib.gh import gh as real_gh

PROJECT_TITLE = "Server Issue Tracking"
STATUS, SEVERITY, NOT_BEFORE = "Status", "Severity", "Not before"
TODO, IN_PROGRESS, DONE = "Todo", "In Progress", "Done"
CHUNK = 25
# GitHub answered HTTP 502 to one request carrying 25 adds and to one carrying 75 field writes.
MUTATIONS_PER_CALL = 10

UNCHANGED, UPDATED, FAILED = "unchanged", "updated", "failed"

_FIELD_VALUE = """
          ... on ProjectV2ItemFieldSingleSelectValue {
            name field { ... on ProjectV2FieldCommon { name } } }
          ... on ProjectV2ItemFieldDateValue {
            date field { ... on ProjectV2FieldCommon { name } } }"""

_ISSUE = (
    """
fragment I on Issue {
  id number state
  labels(first: 100) { nodes { name } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } }
  projectItems(first: 20) {
    nodes { id project { id } fieldValues(first: 30) { nodes {"""
    + _FIELD_VALUE
    + """ } } }
  }
}"""
)

# Fields are listed rather than fetched with `field(name:)`: that form fails the whole
# query with NOT_FOUND when the board lacks the field.
_BOARD = """
    owner { ... on ProjectV2Owner { projectsV2(first: 10, query: $title) { nodes {
      id title
      fields(first: 50) { nodes {
        ... on ProjectV2Field { id name }
        ... on ProjectV2SingleSelectField { id name options { id name } }
      } }
    } } } }"""


@dataclass(frozen=True)
class Field:
    """One board field findings.py writes.

    Attributes:
        id: the field's node id.
        options: option name to option id for a single-select field, None for a date.
    """

    id: str
    options: dict[str, str] | None


@dataclass(frozen=True)
class Board:
    """The board's node id and the fields findings.py writes that it carries."""

    id: str
    fields: dict[str, Field]


@dataclass(frozen=True)
class BoardIssue:
    """One issue, and its item on the board.

    Attributes:
        number: the issue number.
        content_id: the issue's node id, which adding it to the board takes.
        wanted: field name to the value the issue's state and labels call for, None for empty.
        item_id: the issue's item on the board, or None when it is not on it.
        held: field name to the value the item holds; a field it leaves empty is absent.
    """

    number: int
    content_id: str
    wanted: dict[str, str | None]
    item_id: str | None
    held: dict[str, str]


def wanted_fields(state: str, labels: set[str], open_prs: int) -> dict[str, str | None]:
    """The value each board field takes for an issue in ``state`` carrying ``labels``."""
    if state != "OPEN":
        status = DONE
    elif "claimed" in labels or open_prs:
        status = IN_PROGRESS
    else:
        status = TODO
    severity = next(
        (n.split("/", 1)[1] for n in sorted(labels) if n.startswith("severity/")), None
    )
    day = not_before({"labels": [{"name": n} for n in labels]})
    return {
        STATUS: status,
        SEVERITY: severity,
        NOT_BEFORE: day.isoformat() if day else None,
    }


def parse_board(repository: dict) -> Board | None:
    """The board titled exactly `PROJECT_TITLE` from a read's `repository`, or None.

    The `projectsV2(query:)` search is fuzzy, so a board named "Server Issue Tracking
    archive" must not take the writes.
    """
    owner = repository.get("owner") or {}
    for node in (owner.get("projectsV2") or {}).get("nodes") or []:
        if node and node.get("title") == PROJECT_TITLE:
            fields = {}
            for f in node["fields"]["nodes"]:
                if f and f.get("name") in (STATUS, SEVERITY, NOT_BEFORE):
                    options = f.get("options")
                    fields[f["name"]] = Field(
                        f["id"],
                        None
                        if options is None
                        else {o["name"]: o["id"] for o in options},
                    )
            return Board(node["id"], fields)
    return None


def parse_issue(node: dict, board: Board) -> BoardIssue:
    """One aliased issue from a read, with only its item on ``board``."""
    labels = {lab["name"] for lab in node["labels"]["nodes"]}
    open_prs = sum(
        1
        for pr in node["closedByPullRequestsReferences"]["nodes"]
        if pr and pr.get("state") == "OPEN"
    )
    item_id, held = None, {}
    for item in node["projectItems"]["nodes"]:
        if item["project"]["id"] == board.id:
            item_id = item["id"]
            for value in item["fieldValues"]["nodes"]:
                name = ((value or {}).get("field") or {}).get("name")
                if name:
                    held[name] = value.get("name") or value.get("date")
    return BoardIssue(
        node["number"],
        node["id"],
        wanted_fields(node["state"], labels, open_prs),
        item_id,
        held,
    )


def plan_issue(issue: BoardIssue, board: Board) -> tuple[list[str], list[str]]:
    """The fields to write for ``issue``, and why any field cannot be written.

    Returns:
        The names of the fields whose board value differs from `issue.wanted`, in the
        order `wanted` lists them, and one line per field the board cannot take.
    """
    changes, problems = [], []
    for name, want in issue.wanted.items():
        field = board.fields.get(name)
        if field is None:
            problems.append(f'board has no "{name}" field')
            continue
        if want is not None and field.options is not None and want not in field.options:
            problems.append(f'board field "{name}" has no option "{want}"')
            continue
        if issue.held.get(name) != want:
            changes.append(name)
    return changes, problems


def describe(issue: BoardIssue, changes: list[str]) -> str:
    """One change summary, e.g. `added; Status Todo -> In Progress`."""
    parts = [] if issue.item_id else ["added"]
    for name in changes:
        before = issue.held.get(name) or "empty"
        parts.append(f"{name} {before} -> {issue.wanted[name] or 'empty'}")
    return "; ".join(parts)


Gh = Callable[..., subprocess.CompletedProcess[str]]


def _graphql(gh: Gh, query: str, **variables: str) -> dict:
    argv = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        argv += ["-F", f"{key}={value}"]
    return json.loads(gh(*argv).stdout)


def _read(gh: Gh, numbers: Sequence[int], owner: str, name: str) -> dict:
    lookups = "\n".join(f"    i{n}: issue(number: {n}) {{ ...I }}" for n in numbers)
    query = (
        "query($owner: String!, $name: String!, $title: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{lookups}\n{_BOARD}\n  }}\n}}\n{_ISSUE}"
    )
    return _graphql(gh, query, owner=owner, name=name, title=PROJECT_TITLE)


def _field_write(board: Board, item: str, name: str, want: str | None) -> str:
    field = board.fields[name]
    ids = f"projectId: {json.dumps(board.id)}, itemId: {json.dumps(item)}, "
    ids += f"fieldId: {json.dumps(field.id)}"
    if want is None:
        return f"clearProjectV2ItemFieldValue(input: {{{ids}}}) {{ projectV2Item {{ id }} }}"
    value = (
        f"date: {json.dumps(want)}"
        if field.options is None
        else f"singleSelectOptionId: {json.dumps(field.options[want])}"
    )
    return (
        f"updateProjectV2ItemFieldValue(input: {{{ids}, value: {{{value}}}}}) "
        "{ projectV2Item { id } }"
    )


def _write(gh: Gh, board: Board, plans: list[tuple[BoardIssue, list[str]]]) -> None:
    items = {issue.number: issue.item_id for issue, _ in plans if issue.item_id}
    adds = [
        f"a{issue.number}: addProjectV2ItemById(input: {{projectId: "
        f"{json.dumps(board.id)}, contentId: {json.dumps(issue.content_id)}}}) "
        "{ item { id } }"
        for issue, _ in plans
        if issue.item_id is None
    ]
    for data in _mutate(gh, adds):
        for key, value in data.items():
            items[int(key[1:])] = value["item"]["id"]
    writes = [
        f"f{issue.number}_{i}: "
        + _field_write(board, items[issue.number], name, issue.wanted[name])
        for issue, changes in plans
        for i, name in enumerate(changes)
    ]
    for _ in _mutate(gh, writes):
        pass


def _mutate(gh: Gh, aliased: list[str]) -> list[dict]:
    """Runs ``aliased`` mutations `MUTATIONS_PER_CALL` to a request; returns each `data`."""
    return [
        _graphql(gh, "mutation {\n" + "\n".join(batch) + "\n}")["data"]
        for start in range(0, len(aliased), MUTATIONS_PER_CALL)
        if (batch := aliased[start : start + MUTATIONS_PER_CALL])
    ]


def _first_line(exc: Exception) -> str:
    detail = getattr(exc, "stderr", None) or str(exc) or type(exc).__name__
    return detail.strip().partition("\n")[0]


def sync_board(
    numbers: Sequence[int],
    repo: str | None,
    *,
    dry_run: bool = False,
    gh: Gh = real_gh,
) -> dict[int, tuple[str, str]]:
    """Makes the board item of each issue in ``numbers`` agree with the issue.

    Adds an issue the board lacks, then writes each field whose value differs. Never
    raises on a gh or board refusal: that issue's outcome says what went wrong.

    Args:
        numbers: the issues to sync, in the register ``repo`` names.
        repo: `owner/name`, or None for `PR_REPO`.
        dry_run: read and plan, write nothing.
        gh: runs one gh command; a test passes a fake.

    Returns:
        Issue number to `(outcome, detail)`. The outcome is `updated`, `unchanged` or
        `failed`; the detail is the change summary, or the reason for a failure. A dry run
        reports what it would update as `updated`.
    """
    owner, name = (repo or PR_REPO).split("/", 1)
    outcomes: dict[int, tuple[str, str]] = {}
    ordered = sorted(set(numbers))
    for start in range(0, len(ordered), CHUNK):
        chunk = ordered[start : start + CHUNK]
        try:
            repository = _read(gh, chunk, owner, name)["data"]["repository"]
            board = parse_board(repository)
            if board is None:
                raise LookupError(f'no Project titled "{PROJECT_TITLE}"')
            plans = []
            for n in chunk:
                node = repository.get(f"i{n}")
                if node is None:
                    outcomes[n] = (FAILED, "not an issue")
                    continue
                issue = parse_issue(node, board)
                changes, problems = plan_issue(issue, board)
                if problems:
                    outcomes[n] = (FAILED, "; ".join(problems))
                elif changes or issue.item_id is None:
                    plans.append((issue, changes))
                    outcomes[n] = (UPDATED, describe(issue, changes))
                else:
                    outcomes[n] = (UNCHANGED, "")
            if plans and not dry_run:
                _write(gh, board, plans)
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            LookupError,
        ) as exc:
            reason = _first_line(exc)
            for n in chunk:
                if outcomes.get(n, (None,))[0] != FAILED:
                    outcomes[n] = (FAILED, reason)
    return outcomes


def board_warning(outcomes: dict[int, tuple[str, str]]) -> str | None:
    """The one stderr line naming the issues whose board item could not be synced, or None."""
    failed = {n: why for n, (outcome, why) in outcomes.items() if outcome == FAILED}
    if not failed:
        return None
    refs = ", ".join(f"#{n}" for n in sorted(failed))
    reason = failed[min(failed)]
    return (
        f"warning: board not updated for {refs}: {reason}; "
        f"repair with `findings.py sync-board {' '.join(map(str, sorted(failed)))}`"
    )
