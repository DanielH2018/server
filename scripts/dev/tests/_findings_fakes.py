"""Fakes for findings.py's three boundaries: canned gh answers keyed by argv, calls recorded.

`gh_json` dispatches on the first two argv elements — `issue list`, `label list`, `issue
view` — so a test names the answer it wants without restating the argv that `load_issues` and
`_existing_labels` assemble. That is the point of keying it this way: monkeypatching those
functions wholesale means no test runs the argv they build or the label planning that reads
their result. An argv pair no fake answers is an AssertionError rather than an empty list,
because a silent `[]` reads as "the register is empty" and would pass most of these
assertions. `issue view` with no `view=` set is the same mistake wearing a different mask —
`None` would reach `cmd_touch` as an issue with no state — so it asserts too.

`Calls` records BOTH boundaries, as lists either way. Several tests exist to prove nothing
was written, and the label read is a `gh_json` call — recording only `gh` would let them
pass while gh ran.

`make_issue` builds the gh issue shape every findings test asserts against; the `issue`
fixture in conftest.py is how a test reaches it.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from dev.findings_lib.issue_model import DOMAINS, LABELS, Issue, parse_issue
from dev.fanout_lib.batch_holds import BatchHold
from dev.findings_lib.boundaries import FindingsTools
from lib.worktrees import Worktree, held_by_readable_process

CREATED_URL = "https://github.com/o/r/issues/42"

# The claim protocol is fail-closed: a comment with no author metadata folds into no claim.
OPERATOR = "DanielH2018"
FOREIGNER = "drive-by-account"


def operator_comment(body: str, **fields) -> dict:
    """A comment the operator wrote — the only kind whose `Claim:` trailer counts."""
    return {
        "body": body,
        "author": {"login": OPERATOR},
        "authorAssociation": "OWNER",
        "viewerDidAuthor": True,
        **fields,
    }


def foreign_comment(body: str, **fields) -> dict:
    """A comment any GitHub account could post on this PUBLIC repo's issues.

    `authorAssociation` is `NONE` and `viewerDidAuthor` is False, which is what gh returns
    for a drive-by commenter. Nothing it says may open, close or age a claim.
    """
    return {
        "body": body,
        "author": {"login": FOREIGNER},
        "authorAssociation": "NONE",
        "viewerDidAuthor": False,
        **fields,
    }


def make_issue(
    number,
    *,
    state="OPEN",
    labels=(),
    fp=None,
    comments=(),
    created="2026-08-15T10:00:00Z",
    title="t",
    author=OPERATOR,
):
    """One gh issue object, with the fingerprint trailer `open` writes when ``fp`` is given.

    A `comments` entry given as a string becomes an OPERATOR comment, since that is what a
    claim test almost always means; pass `foreign_comment(...)` (or any dict) to build one
    the claim protocol must ignore.

    `claude` is always in the labels, because every issue in the register carries it —
    `load_issues` filters on it — and `plan_claim` refuses an issue without it. A
    fixture that omitted it would be refused for a reason no test meant to state.
    """
    labels = ("claude", *(n for n in labels if n != "claude"))
    body = (
        f"details\n\n---\nFingerprint: `{fp}`\nSource: review-2026-08-15\n"
        if fp
        else "details"
    )
    return {
        "number": number,
        "author": {"login": author},
        "title": title,
        "state": state,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "createdAt": created,
        "url": f"https://github.com/o/r/issues/{number}",
        "comments": [
            c if isinstance(c, dict) else operator_comment(c) for c in comments
        ],
        "blockedBy": {"nodes": []},
    }


def typed(raw: dict) -> Issue:
    """``raw``, a gh issue object such as `make_issue` builds, parsed into an `Issue`.

    A `fields` key, which gh's JSON never carries but `attach_fields` fills, becomes
    `Issue.fields`, so a test of a pure reader can set an issue field without the graphql fake.
    """
    return replace(parse_issue(raw), fields=raw.get("fields") or {})


@dataclass
class Calls:
    """Every boundary call in order, `gh` and `gh_json` argv alike as lists."""

    gh: list[list[str]] = field(default_factory=list)
    gh_json: list[list[str]] = field(default_factory=list)
    # The checkout each worktree read was aimed at; None is this repo's own.
    checkouts: list[str | None] = field(default_factory=list)
    # The (orchestrator branch, repo) each manifest read was asked for.
    launched: list[tuple[str, str]] = field(default_factory=list)
    # The (repo, None for this one; stale issue numbers) each running-batch read was asked for.
    batch_holds: list[tuple[str, list[int]]] = field(default_factory=list)
    # The (issue numbers, repo) each board sync was asked for.
    board: list[tuple[list[int], str | None]] = field(default_factory=list)
    # The (branch, OWNER/NAME) each session-milestone lookup was asked for.
    milestones: list[tuple[str, str]] = field(default_factory=list)
    milestone_closes: list[tuple[str, dict[str, bool]]] = field(default_factory=list)

    def none(self) -> bool:
        """Whether neither boundary was reached at all."""
        return not self.gh and not self.gh_json


@dataclass
class Fakes:
    """What each boundary answers; every field is a per-test override.

    `labels=None` means the repo already carries every label `plan_sync_labels` knows, which
    is what all but one of the `open` tests need.
    """

    issues: list[dict] = field(default_factory=list)
    labels: set[str] | None = None
    view: dict | None = None
    # Answers the `pr list` key, for Task 5's `next`.
    prs: list[dict] = field(default_factory=list)
    url: str = CREATED_URL
    # Left None it asserts: the default 'no worktrees, git ok' would make every claim read stale.
    worktree_facts: Callable[[], tuple] | None = None
    launched: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    batch_holds: dict[int, BatchHold] = field(default_factory=dict)
    # Consumed on the first match, so a later call with the same pair answers normally.
    gh_errors: dict[str, BaseException] = field(default_factory=dict)
    json_errors: dict[str, BaseException] = field(default_factory=dict)
    # The register's tracked files. Empty by default, so no test's `paths` depend on
    # what this tree tracks.
    tracked: frozenset[str] = frozenset()
    board: dict[int, tuple[str, str]] = field(default_factory=dict)
    api: dict[str, list] = field(default_factory=dict)
    milestone_titles: dict[str, str] = field(default_factory=dict)
    # The paths a live process works in, the /proc snapshot the real hold reader matches.
    held_paths: set[str] = field(default_factory=set)
    open_milestones: list[str] = field(default_factory=list)


def facts(trees=(), *, dirty=False, merged=False, ok=True):
    """A `tools.worktree_facts` replacement: the four values as constants.

    `dirty` and `merged` are the verdicts `classify` reads, so `facts([tree], dirty=True)` is
    a worktree still holding work and `facts([tree], merged=True)` is one whose PR landed.
    `ok=False` is a FAILED git read, which every caller treats differently from an empty list.
    """
    return lambda: (list(trees), lambda _p: dirty, lambda _t: merged, ok)


def live_worktree(branch: str):
    """A `facts(...)` whose single worktree is LOCKED by a session `classify` calls alive.

    `session_is_alive` treats a lock reason it cannot parse as live — "an unrecognized lock
    is someone else's, and guessing wrong destroys work" — so a plain reason string is the
    shortest live fixture there is, and `classify` short-circuits to KEEP before it looks at
    merged or dirty. This is what `cmd_claim`'s stale-at-birth guard needs to let a claim
    through; `facts()` with no worktrees is its refusing counterpart.
    """
    tree = Worktree(
        path=f"/w/{branch}", head="abc", branch=branch, locked=True, lock_reason="held"
    )
    return facts([tree])


def _issues_named(f: Fakes, number: int) -> list[dict]:
    """Every issue dict known to ``f`` carrying this number, `issues` and `view` alike.

    A test builds `view` from the same object it put in `issues`, so mutating through one
    reaches both — this also covers the rare case where they are separate objects. A
    per-number read SEQUENCE (a list rather than an issue dict, see `build_tools`'s `issue
    view` branch) is skipped here: it stands for several successive reads, not one mutable
    object, so there is nothing here to append a posted comment onto.
    """
    found = [i for i in f.issues if i.get("number") == number]
    if isinstance(f.view, dict):
        candidates = f.view.values() if "number" not in f.view else [f.view]
        found += [
            c
            for c in candidates
            if isinstance(c, dict) and c.get("number") == number and c not in found
        ]
    return found


def _as_gh_returns(issue: dict) -> dict:
    """``issue`` without its `fields`, which `gh issue list --json` cannot return.

    Only `gh_calls.attach_fields` puts them back, so a loader that skips it fails here as it
    would against GitHub. The lists inside are shared, so a posted comment still reaches the
    fixture.
    """
    return {k: v for k, v in issue.items() if k != "fields"}


def aimed_at(calls: Calls, repo: str) -> bool:
    """Whether every gh call went to ``repo``: `--repo` on a subcommand, `-F name=` on GraphQL.

    A mutation addresses its issue by node id, which the read that found it already aimed.
    """

    def aimed(argv: list[str]) -> bool:
        if argv[:2] == ["api", "graphql"]:
            return (
                "query=mutation" in argv[3][:20] or f"name={repo.split('/')[1]}" in argv
            )
        return argv[-2:] == ["--repo", repo]

    return all(aimed(c) for c in calls.gh + calls.gh_json)


ISSUE_FIELD_DEFS = [
    {
        "id": "IF_sev",
        "name": "Severity",
        "options": [{"id": f"o_{s}", "name": s} for s in ("High", "Medium", "Low")],
    },
    {"id": "IF_nb", "name": "Not Before"},
    {
        "id": "IF_dom",
        "name": "Domain",
        "options": [{"id": f"o_{d}", "name": d} for d in DOMAINS],
    },
]
ISSUE_TYPE_DEFS = [
    {"id": f"IT_{name}", "name": name}
    for name in ("Task", "Bug", "Feature", "Gap", "Improvement")
]


def _graphql_answer(f: Fakes, query_arg: str) -> dict:
    """What `gh api graphql` answers for the three queries `gh_calls` sends.

    The aliased field read answers each issue's `fields` key, so a fixture sets its issue
    fields as `{"Severity": "High", "Not Before": "2026-12-01"}`. `set_fields`' read answers
    `ISSUE_FIELD_DEFS` and `ISSUE_TYPE_DEFS`, and its mutation answers nothing;
    `calls.gh_json` holds it.
    """
    query = query_arg.removeprefix("query=")
    if query.startswith("mutation"):
        return {"data": {}}
    if "issueFields(first" in query:
        (number,) = map(int, re.findall(r"issue\(number: (\d+)\)", query))
        return {
            "data": {
                "repository": {
                    "issue": {"id": f"I_{number}"},
                    "issueFields": {"nodes": ISSUE_FIELD_DEFS},
                    "issueTypes": {"nodes": ISSUE_TYPE_DEFS},
                }
            }
        }
    repository = {}
    for n in map(int, re.findall(r"i(\d+): issue\(", query)):
        held = next(
            (i.get("fields") for i in _issues_named(f, n) if i.get("fields")), {}
        )
        nodes = [
            {"value": v, "field": {"name": k}}
            if k == "Not Before"
            else {"name": v, "field": {"name": k}}
            for k, v in (held or {}).items()
        ]
        repository[f"i{n}"] = {"issueFieldValues": {"nodes": nodes}}
    return {"data": {"repository": repository}}


def build_tools(f: Fakes | None = None) -> tuple[FindingsTools, Calls]:
    """A `FindingsTools` answering from `f`, and the record of what it was asked."""
    f = f or Fakes()
    calls = Calls()
    gh_errors = dict(f.gh_errors)
    view_reads: dict[int, int] = {}

    def gh_json(*argv, **kwargs):
        calls.gh_json.append(list(argv))
        if argv[:2] == ("api", "graphql"):
            if "api graphql" in f.json_errors:
                raise f.json_errors["api graphql"]
            return _graphql_answer(f, argv[argv.index("-f") + 1])
        if argv[0] == "api":
            assert "--repo" not in argv, f"gh api rejects --repo: {argv}"
            path = argv[1].split("?")[0]
            if f"api {path}" in f.json_errors:
                raise f.json_errors[f"api {path}"]
            if path not in f.api:
                raise AssertionError(argv)
            if path.endswith("/milestones") and "state=" in argv[1]:
                state = argv[1].split("state=")[1].split("&")[0]
                return [
                    [m for m in page if m["state"] == state] for page in f.api[path]
                ]
            return list(f.api[path])
        key = " ".join(argv[:2])
        if key in f.json_errors:
            raise f.json_errors[key]
        if key == "label list":
            names = LABELS if f.labels is None else f.labels
            return [{"name": name} for name in names]
        if key == "issue view":
            if f.view is None:
                raise AssertionError(argv)
            if isinstance(f.view, dict) and "number" not in f.view:
                try:
                    value = f.view[int(argv[2])]
                except KeyError, ValueError:
                    raise AssertionError(argv) from None
            else:
                value = f.view
            if isinstance(value, list):
                # A per-number sequence that sticks on the last entry: the read-back needs a second
                # read that disagrees with the first.
                number = int(argv[2])
                i = view_reads.get(number, 0)
                view_reads[number] = i + 1
                return _as_gh_returns(value[min(i, len(value) - 1)])
            return _as_gh_returns(value)
        if key == "issue list":
            return [_as_gh_returns(i) for i in f.issues]
        if key == "pr list":
            return list(f.prs)
        raise AssertionError(argv)

    def gh(*argv, **kwargs):
        calls.gh.append(list(argv))
        error = gh_errors.pop(" ".join(argv[:2]), None)
        if error is not None:
            raise error
        if list(argv[:2]) == ["issue", "comment"]:
            body = argv[argv.index("--body") + 1]
            for issue in _issues_named(f, int(argv[2])):
                # Stamped as the operator's: gh posts as the authenticated account, so its claim
                # trailer counts.
                issue.setdefault("comments", []).append(operator_comment(body))
        return subprocess.CompletedProcess(list(argv), 0, f"{f.url}\n", "")

    def worktree_facts(checkout=None):
        calls.checkouts.append(checkout)
        if f.worktree_facts is None:
            raise AssertionError(
                "this command reads the worktrees; pass Fakes(worktree_facts=facts(...))"
            )
        return f.worktree_facts()

    def launched_branches(branch, repo):
        calls.launched.append((branch, repo))
        return set(f.launched.get((branch, repo), set()))

    def running_batch_holds(repo, issues):
        calls.batch_holds.append((repo, sorted(issues)))
        return {n: why for n, why in f.batch_holds.items() if n in issues}

    board = f.board

    def sync_board(numbers, repo, *, dry_run=False):
        calls.board.append((list(numbers), repo))
        return {n: board.get(n, ("unchanged", "")) for n in numbers}

    def attach_milestone(branch, repo, *, dry_run=False):
        calls.milestones.append((branch, repo))
        return f.milestone_titles.get(branch)

    def close_idle_milestones(repo, held, *, dry_run=False):
        verdicts = {t: held(t) for t in f.open_milestones}
        calls.milestone_closes.append((repo, verdicts))
        return [t for t, h in verdicts.items() if not h]

    return (
        FindingsTools(
            gh_json,
            gh,
            worktree_facts,
            launched_branches=launched_branches,
            running_batch_holds=running_batch_holds,
            sync_board=sync_board,
            attach_milestone=attach_milestone,
            close_idle_milestones=close_idle_milestones,
            tracked=lambda: f.tracked,
            tree_held=lambda path: held_by_readable_process(
                path, holds=lambda: [(1, p, "cwd") for p in f.held_paths]
            ),
        ),
        calls,
    )
