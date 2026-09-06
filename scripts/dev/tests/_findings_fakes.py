"""Fakes for findings.py's three boundaries: canned gh answers keyed by argv, calls recorded.

`gh_json` dispatches on the first two argv elements — `issue list`, `label list`,
`issue view` — so a test names the answer it wants without restating the argv that
`load_issues` and `_existing_labels` assemble. That is the point of keying it this way:
both of those functions were monkeypatched wholesale until 2026-09-04, so no test ever ran
the argv they build or the label planning that reads their result. An argv pair no fake
answers is an AssertionError rather than an empty list, because a silent `[]` reads as "the
register is empty" and would pass most of these assertions. `issue view` with no `view=` set
is the same mistake wearing a different mask — `None` would reach `cmd_touch` as an issue
with no state — so it asserts too.

`Calls` records BOTH boundaries, as lists either way. Several tests exist to prove nothing
was written, and the label read is a `gh_json` call — recording only `gh` would let them
pass while gh ran.

`make_issue` builds the gh issue shape every findings test asserts against; the `issue`
fixture in conftest.py is how a test reaches it.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from dev.findings_lib.issue_model import LABELS
from dev.findings_lib.boundaries import FindingsTools
from dev.prune_worktrees import Worktree

CREATED_URL = "https://github.com/o/r/issues/42"

# The author metadata `gh` returns on every comment, and which `is_operator_comment` reads.
# Fixtures stamp it because the claim protocol is FAIL-CLOSED: a comment carrying no author
# metadata folds into no claim at all, so a bare `{"body": ...}` reads as an unclaimed issue.
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
    for a drive-by commenter. Nothing it says may open, close or age a claim (#1280).
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
):
    """One gh issue object, with the fingerprint trailer `open` writes when ``fp`` is given.

    A `comments` entry given as a string becomes an OPERATOR comment, since that is what a
    claim test almost always means; pass `foreign_comment(...)` (or any dict) to build one
    the claim protocol must ignore.

    `claude` is always in the labels, because every issue in the register carries it —
    `load_issues` filters on it — and `plan_claim` refuses an issue without it (#1277). A
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
        "title": title,
        "state": state,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "createdAt": created,
        "url": f"https://github.com/o/r/issues/{number}",
        "comments": [
            c if isinstance(c, dict) else operator_comment(c) for c in comments
        ],
    }


@dataclass
class Calls:
    """Every boundary call in order, `gh` and `gh_json` argv alike as lists."""

    gh: list[list[str]] = field(default_factory=list)
    gh_json: list[list[str]] = field(default_factory=list)

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
    # A single issue dict, or a dict keyed by issue number for a test that drives `claim` or
    # `release` against more than one issue in one `main()` call.
    view: dict | None = None
    # Answers the `pr list` key, for Task 5's `next`.
    prs: list[dict] = field(default_factory=list)
    url: str = CREATED_URL
    # Keyed by the `issue create` / `issue close` pair `gh` is called with, the way
    # `json_errors` is keyed, and CONSUMED on the first match rather than raised every time.
    # That asymmetry is what `_create_with_optional_project` needs: its retry drops
    # `--project` from an argv whose first two elements are still `issue create`, so an
    # error that fired on every match would fail the retry the test exists to prove.
    # The claim-staleness read, as `facts(...)` builds it. Left None it ASSERTS rather than
    # answering "no worktrees, git ok" — that particular answer makes every claim read stale,
    # so a test that got it by default would pass for a reason it never stated. Same argument
    # as the unanswered-argv assertion above.
    worktree_facts: Callable[[], tuple] | None = None
    gh_errors: dict[str, BaseException] = field(default_factory=dict)
    # Keyed by the same `issue list` / `label list` / `issue view` pair `gh_json` dispatches
    # on, so a test can fail the issue read while the label read still answers.
    json_errors: dict[str, BaseException] = field(default_factory=dict)


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
    through (#1278, #1281); `facts()` with no worktrees is its refusing counterpart.
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


def build_tools(f: Fakes | None = None) -> tuple[FindingsTools, Calls]:
    """A `FindingsTools` answering from `f`, and the record of what it was asked."""
    f = f or Fakes()
    calls = Calls()
    gh_errors = dict(f.gh_errors)
    view_reads: dict[int, int] = {}

    def gh_json(*argv, **kwargs):
        calls.gh_json.append(list(argv))
        key = " ".join(argv[:2])
        if key in f.json_errors:
            raise f.json_errors[key]
        if key == "label list":
            names = LABELS if f.labels is None else f.labels
            return [{"name": name} for name in names]
        if key == "issue view":
            if f.view is None:
                raise AssertionError(argv)
            # A single issue dict carries a "number" key; a dict keyed by issue number does
            # not, so that key tells the two shapes apart without a second field.
            if isinstance(f.view, dict) and "number" not in f.view:
                try:
                    value = f.view[int(argv[2])]
                except KeyError, ValueError:
                    raise AssertionError(argv) from None
            else:
                value = f.view
            if isinstance(value, list):
                # A per-number SEQUENCE: one entry per successive `issue view` call for
                # this number, sticking on the last once exhausted. `cmd_claim`'s
                # post-write read-back needs the SECOND read to disagree with the first —
                # a rival's claim landing between the pre-write check and the read-back —
                # which a single static view cannot express.
                number = int(argv[2])
                i = view_reads.get(number, 0)
                view_reads[number] = i + 1
                return value[min(i, len(value) - 1)]
            return value
        if key == "issue list":
            return list(f.issues)
        if key == "pr list":
            return list(f.prs)
        raise AssertionError(argv)

    def gh(*argv, **kwargs):
        calls.gh.append(list(argv))
        error = gh_errors.pop(" ".join(argv[:2]), None)
        if error is not None:
            raise error
        if list(argv[:2]) == ["issue", "comment"]:
            # `cmd_claim` reads the issue back after posting its claim comment, to catch a
            # race a second worktree won. Reflecting the write here is what makes that
            # read-back see the comment it just posted, the way real `gh` would.
            body = argv[argv.index("--body") + 1]
            for issue in _issues_named(f, int(argv[2])):
                # Stamped as the operator's: `gh` posts as the authenticated account, so a
                # comment this script wrote is one whose claim trailer counts.
                issue.setdefault("comments", []).append(operator_comment(body))
        return subprocess.CompletedProcess(list(argv), 0, f"{f.url}\n", "")

    def worktree_facts():
        if f.worktree_facts is None:
            raise AssertionError(
                "this command reads the worktrees; pass Fakes(worktree_facts=facts(...))"
            )
        return f.worktree_facts()

    return (
        FindingsTools(gh_json, gh, worktree_facts),
        calls,
    )
