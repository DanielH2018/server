"""The Server Issue Tracking board, kept equal to each issue's state and claim.

The board holds one field findings.py owns, derived from the issue alone:

    Status      Done when the issue is closed; In Progress while a worktree holds its
                `Claim:` comment or an open pull request will close it; Todo otherwise.

The board's Status is what GitHub shows in an issue's sidebar and logs on its timeline. It
is not the issue's own `status`, a preview this account cannot set, so GitHub reserves the
issue-field name `Status`. Severity and Not Before are issue fields on the issue itself,
which `open` and `defer` write and the board shows as columns, so nothing here derives them.

THE ISSUE STAYS THE RECORD. `next`, `claim` and the fan-out read the issue, never the
board, so a board write is best-effort: `findings.main` syncs every issue a command wrote
to, prints `board_warning`'s one line when that fails, and keeps the command's own exit
code. `findings.py sync-board` is the repair path, and daniel-box's
`homelab-board-sync.timer` runs it hourly for the changes findings.py never saw.

Every call is `gh api graphql`. Issues are read in chunks of aliased lookups. A chunk's
writes go out as aliased mutations, a few to a request: the adds first, because the Status
writes need their item ids.

Typical usage example:

    outcomes = sync_board([8237, 8240], repo=None)
"""

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass


from dev.findings_lib.issue_model import PR_REPO, claim_of, parse_comment
from lib.gh import gh as real_gh

PROJECT_TITLE = "Server Issue Tracking"
STATUS = "Status"
TODO, IN_PROGRESS, DONE = "Todo", "In Progress", "Done"
CHUNK = 25
# GitHub answered HTTP 502 to one request carrying 25 adds and to one carrying 75 field writes.
MUTATIONS_PER_CALL = 10
# Ten writes per 8s stays under GitHub's secondary limit of 80 content writes a minute.
MUTATION_PAUSE_S = 8.0

UNCHANGED, UPDATED, FAILED = "unchanged", "updated", "failed"

_ISSUE = """
fragment I on Issue {
  id number state
  comments(first: 100) { nodes { body createdAt authorAssociation viewerDidAuthor } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } }
  projectItems(first: 20) {
    nodes { id project { id } fieldValues(first: 30) { nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name field { ... on ProjectV2FieldCommon { name } } } } } }
  }
}"""

# Fields are listed, not fetched with `field(name:)`:
# that form fails the whole query with NOT_FOUND when the board lacks the field.
_BOARD = """
    owner { ... on ProjectV2Owner { projectsV2(first: 10, query: $title) { nodes {
      id title
      fields(first: 50) { nodes {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      } }
    } } } }"""


@dataclass(frozen=True)
class Board:
    """The board's node id, and its Status field's id and options when it has one.

    Attributes:
        id: the board's node id.
        status_id: the Status field's node id, or None when the board lacks the field.
        options: Status option name to option id.
    """

    id: str
    status_id: str | None
    options: dict[str, str]


@dataclass(frozen=True)
class BoardIssue:
    """One issue, and its item on the board.

    Attributes:
        number: the issue number.
        content_id: the issue's node id, which adding it to the board takes.
        wanted: the Status the issue's state and claim call for.
        item_id: the issue's item on the board, or None when it is not on it.
        held: the Status the item holds, or None.
    """

    number: int
    content_id: str
    wanted: str
    item_id: str | None
    held: str | None


def wanted_status(state: str, open_prs: int, claimed: bool) -> str:
    """The Status one issue takes.

    Args:
        state: the issue's GraphQL state, `OPEN` or `CLOSED`.
        open_prs: how many open pull requests will close it.
        claimed: whether a worktree holds its `Claim:` comment.
    """
    if state != "OPEN":
        return DONE
    return IN_PROGRESS if claimed or open_prs else TODO


def parse_board(repository: dict) -> Board | None:
    """The board titled exactly `PROJECT_TITLE` from a read's `repository`, or None.

    The `projectsV2(query:)` search is fuzzy, so a board named "Server Issue Tracking
    archive" must not take the writes.
    """
    owner = repository.get("owner") or {}
    for node in (owner.get("projectsV2") or {}).get("nodes") or []:
        if node and node.get("title") == PROJECT_TITLE:
            status = next(
                (f for f in node["fields"]["nodes"] if f and f.get("name") == STATUS),
                None,
            )
            if status is None:
                return Board(node["id"], None, {})
            return Board(
                node["id"],
                status["id"],
                {o["name"]: o["id"] for o in status["options"]},
            )
    return None


def parse_issue(node: dict, board: Board) -> BoardIssue:
    """One aliased issue from a read, with only its item on ``board``."""
    open_prs = sum(
        1
        for pr in node["closedByPullRequestsReferences"]["nodes"]
        if pr and pr.get("state") == "OPEN"
    )
    item_id, held = None, None
    for item in node["projectItems"]["nodes"]:
        if item["project"]["id"] == board.id:
            item_id = item["id"]
            for value in item["fieldValues"]["nodes"]:
                if ((value or {}).get("field") or {}).get("name") == STATUS:
                    held = value.get("name")
    comments = tuple(parse_comment(c) for c in node["comments"]["nodes"])
    claimed = claim_of(comments) is not None
    return BoardIssue(
        node["number"],
        node["id"],
        wanted_status(node["state"], open_prs, claimed),
        item_id,
        held,
    )


def problem(issue: BoardIssue, board: Board) -> str | None:
    """Why the board cannot take ``issue``'s Status, or None when it can."""
    if board.status_id is None:
        return f'board has no "{STATUS}" field'
    if issue.wanted not in board.options:
        return f'board field "{STATUS}" has no option "{issue.wanted}"'
    return None


def describe(issue: BoardIssue) -> str:
    """One change summary, e.g. `added; Status Todo -> In Progress`."""
    parts = [] if issue.item_id else ["added"]
    if issue.held != issue.wanted:
        parts.append(f"{STATUS} {issue.held or 'empty'} -> {issue.wanted}")
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


def _status_write(board: Board, item: str, want: str) -> str:
    return (
        f"updateProjectV2ItemFieldValue(input: {{projectId: {json.dumps(board.id)}, "
        f"itemId: {json.dumps(item)}, fieldId: {json.dumps(board.status_id)}, "
        f"value: {{singleSelectOptionId: {json.dumps(board.options[want])}}}}}) "
        "{ projectV2Item { id } }"
    )


def _paced(gh: Gh, sleep: Callable[[float], None]) -> Gh:
    """``gh`` that waits `MUTATION_PAUSE_S` before every mutation request but the first."""
    sent = False

    def run(*argv: str, **kwargs):
        nonlocal sent
        if any(a.startswith("query=mutation") for a in argv):
            if sent:
                sleep(MUTATION_PAUSE_S)
            sent = True
        return gh(*argv, **kwargs)

    return run


def _write(gh: Gh, board: Board, plans: list[BoardIssue]) -> None:
    items = {issue.number: issue.item_id for issue in plans if issue.item_id}
    adds = [
        f"a{issue.number}: addProjectV2ItemById(input: {{projectId: "
        f"{json.dumps(board.id)}, contentId: {json.dumps(issue.content_id)}}}) "
        "{ item { id } }"
        for issue in plans
        if issue.item_id is None
    ]
    for data in _mutate(gh, adds):
        for key, value in data.items():
            items[int(key[1:])] = value["item"]["id"]
    writes = [
        f"f{issue.number}: " + _status_write(board, items[issue.number], issue.wanted)
        for issue in plans
        if issue.held != issue.wanted
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
    sleep: Callable[[float], None] = time.sleep,
) -> dict[int, tuple[str, str]]:
    """Makes the board item of each issue in ``numbers`` agree with the issue.

    Adds an issue the board lacks, then writes its Status when that differs. Never raises
    on a gh or board refusal: that issue's outcome says what went wrong.

    Args:
        numbers: the issues to sync, in the register ``repo`` names.
        repo: `owner/name`, or None for `PR_REPO`.
        dry_run: read and plan, write nothing.
        gh: runs one gh command; a test passes a fake.
        sleep: the pause between mutation requests; a test passes a recorder.

    Returns:
        Issue number to `(outcome, detail)`. The outcome is `updated`, `unchanged` or
        `failed`; the detail is the change summary, or the reason for a failure. A dry run
        reports what it would update as `updated`.
    """
    owner, name = (repo or PR_REPO).split("/", 1)
    gh = _paced(gh, sleep)
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
                why = problem(issue, board)
                if why:
                    outcomes[n] = (FAILED, why)
                elif issue.held != issue.wanted or issue.item_id is None:
                    plans.append(issue)
                    outcomes[n] = (UPDATED, describe(issue))
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
