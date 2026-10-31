"""The "Not before" Date field on the Claude findings board, kept equal to the label.

GitHub's issue list cannot match a label prefix or sort by a label, so the `not-before:` label
alone gives no view of every deferred issue in date order. The Project board can: a Date
field sorts, and a saved view filters it against `@today`. This module copies the label's
date into that field.

THE LABEL STAYS THE RECORD. `next`, `claim` and the fan-out read the label and never this
field. So every write here is best-effort: a failure warns on stderr and leaves the command
that set the label at its own exit code. `board-dates` is the repair path, and it compares
every open issue's field to its label.

Every call is `gh api graphql` through `tools.gh`. The item, project and field ids come only
from the read, and `aimed` appends `--repo` to `tools.gh_json`, which `gh api` rejects.

Typical usage example:

    sync_board_date(6710, date(2026, 11, 7), tools, dry_run=False)
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import load_issues
from dev.findings_lib.issue_model import NOT_BEFORE_FIELD, PROJECT_TITLE, not_before

# Fields are listed rather than fetched with `field(name:)`: that form fails the whole
# query with NOT_FOUND when the board lacks the field, and gh exits 1 with no data used.
_READ = """
query($owner: String!, $name: String!, $number: Int!, $title: String!, $field: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      projectItems(first: 20) {
        nodes {
          id
          project { id }
          fieldValueByName(name: $field) { ... on ProjectV2ItemFieldDateValue { date } }
        }
      }
    }
    owner {
      ... on ProjectV2Owner {
        projectsV2(first: 10, query: $title) {
          nodes { id title fields(first: 50) { nodes { ... on ProjectV2Field { id name } } } }
        }
      }
    }
  }
}"""

_ADD = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

_SET = """
mutation($project: ID!, $item: ID!, $field: ID!, $date: Date!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: {date: $date}}
  ) { projectV2Item { id } }
}"""

_CLEAR = """
mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field}
  ) { projectV2Item { id } }
}"""


@dataclass(frozen=True)
class BoardState:
    """One issue as the board holds it.

    Attributes:
        content_id: the issue's node id, which adding it to the board takes.
        project_id: the board's node id, or None when no Project has `PROJECT_TITLE`.
        field_id: the `NOT_BEFORE_FIELD` field's id, or None when the board lacks it.
        item_id: the issue's item on that board, or None when it is not on it.
        current: the date the field holds for the issue, or None.
    """

    content_id: str
    project_id: str | None
    field_id: str | None
    item_id: str | None
    current: date | None


def parse_board_state(data: dict) -> BoardState:
    """Reads a `_READ` response into a `BoardState`.

    The `projectsV2(query:)` search is fuzzy, so the board is the one whose title is EXACTLY
    `PROJECT_TITLE`; a board named "Claude findings archive" must not take the write.
    """
    repo = data["data"]["repository"]
    issue = repo["issue"]
    boards = (repo.get("owner") or {}).get("projectsV2", {}).get("nodes", [])
    board = next((b for b in boards if b and b["title"] == PROJECT_TITLE), None)
    project_id = board["id"] if board else None
    field_id = None
    if board:
        fields = board.get("fields", {}).get("nodes", [])
        field_id = next(
            (f["id"] for f in fields if f and f.get("name") == NOT_BEFORE_FIELD), None
        )
    item_id, current = None, None
    for item in issue["projectItems"]["nodes"]:
        if item["project"]["id"] == project_id:
            item_id = item["id"]
            value = item.get("fieldValueByName") or {}
            current = date.fromisoformat(value["date"]) if value.get("date") else None
    return BoardState(issue["id"], project_id, field_id, item_id, current)


def plan_board_date(state: BoardState, want: date | None) -> list[str] | str:
    """The steps that make the board's date for an issue equal ``want``.

    Returns:
        A list drawn from ``add``, ``set`` and ``clear``, in the order to run them, and
        empty when the board already agrees. A string instead when the board cannot take
        the date at all, naming why and how to fix it.
    """
    if state.project_id is None:
        return f'no Project titled "{PROJECT_TITLE}"'
    if state.field_id is None:
        return (
            f'Project "{PROJECT_TITLE}" has no "{NOT_BEFORE_FIELD}" field; create it with '
            f"`gh project field-create <number> --owner <owner> --name "
            f"'{NOT_BEFORE_FIELD}' --data-type DATE`"
        )
    if state.current == want:
        return []
    if want is None:
        return ["clear"]
    return ["set"] if state.item_id else ["add", "set"]


def _graphql(tools: FindingsTools, query: str, *fields: str) -> dict:
    argv = ["api", "graphql", "-f", f"query={query}"]
    for f in fields:
        argv += ["-F", f]
    return json.loads(tools.gh(*argv).stdout)


def _repo_fields(tools: FindingsTools) -> tuple[str, str]:
    # gh fills `{owner}`/`{repo}` from the working tree's remote, the repo `findings.py`
    # writes to when `--repo` is absent.
    if tools.repo is None:
        return "owner={owner}", "name={repo}"
    owner, name = tools.repo.split("/", 1)
    return f"owner={owner}", f"name={name}"


def sync_board_date(
    number: int, want: date | None, tools: FindingsTools, *, dry_run: bool
) -> str:
    """Makes the board's "Not before" date for issue ``number`` equal ``want``.

    Adds the issue to the board first when it is missing and there is a date to set.
    Warns on stderr rather than raising when gh or the board refuses.

    Returns:
        ``set``, ``cleared``, ``unchanged`` or ``failed``. A dry run reads the board,
        prints the steps it would run, and returns what it would have done.
    """
    try:
        data = _graphql(
            tools,
            _READ,
            *_repo_fields(tools),
            f"number={number}",
            f"title={PROJECT_TITLE}",
            f"field={NOT_BEFORE_FIELD}",
        )
        state = parse_board_state(data)
        steps = plan_board_date(state, want)
        if isinstance(steps, str):
            sys.stderr.write(f"warning: #{number} board date not written: {steps}\n")
            return "failed"
        if dry_run:
            for step in steps:
                print(f"gh api graphql  # {step} #{number} {want or ''}".rstrip())
        else:
            _apply(steps, state, want, tools)
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        first_line = detail.strip().partition("\n")[0]
        sys.stderr.write(f"warning: #{number} board date not written: {first_line}\n")
        return "failed"
    if not steps:
        return "unchanged"
    return "cleared" if steps == ["clear"] else "set"


def _apply(
    steps: list[str], state: BoardState, want: date | None, tools: FindingsTools
) -> None:
    item = state.item_id
    for step in steps:
        if step == "add":
            data = _graphql(
                tools,
                _ADD,
                f"project={state.project_id}",
                f"content={state.content_id}",
            )
            item = data["data"]["addProjectV2ItemById"]["item"]["id"]
        elif step == "set":
            _graphql(
                tools,
                _SET,
                f"project={state.project_id}",
                f"item={item}",
                f"field={state.field_id}",
                f"date={want}",  # a date's str() is its ISO form
            )
        else:
            _graphql(
                tools,
                _CLEAR,
                f"project={state.project_id}",
                f"item={item}",
                f"field={state.field_id}",
            )


def cmd_board_dates(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``board-dates`` subcommand: makes every open issue's board date match.

    Sets the field where a `not-before:` label has no matching date on the board, and
    clears it where the label is gone. It is the backfill for issues deferred before the
    field existed, and the repair for a date someone changed on the board by hand.

    Returns:
        1 if any issue's board date could not be written, 0 otherwise.
    """
    counts = dict.fromkeys(("set", "cleared", "unchanged", "failed"), 0)
    for issue in sorted(load_issues("open", tools), key=lambda i: i["number"]):
        outcome = sync_board_date(
            issue["number"], not_before(issue), tools, dry_run=args.dry_run
        )
        counts[outcome] += 1
        if outcome in ("set", "cleared"):
            print(f"#{issue['number']} {outcome} {not_before(issue) or ''}".rstrip())
    print(", ".join(f"{n} {k}" for k, n in counts.items()))
    return 1 if counts["failed"] else 0
