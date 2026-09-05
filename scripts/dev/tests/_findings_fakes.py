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

from dev.findings_model import LABELS
from dev.findings_tools import FindingsTools, run_verify

CREATED_URL = "https://github.com/o/r/issues/42"


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
    """One gh issue object, with the fingerprint trailer `open` writes when ``fp`` is given."""
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
        "comments": [{"body": c} for c in comments],
    }


def fake_verify(command: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """A verify-by shell that spawns nothing: `true` exits 0, anything else exits 1.

    The CLI tests care which verdict a command produces, not that a shell produced it, and
    a real `/bin/sh` per issue is a process per test for an answer this returns directly.
    """
    return subprocess.CompletedProcess(command, 0 if command == "true" else 1, "", "")


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
    gh_errors: dict[str, BaseException] = field(default_factory=dict)
    # Keyed by the same `issue list` / `label list` / `issue view` pair `gh_json` dispatches
    # on, so a test can fail the issue read while the label read still answers.
    json_errors: dict[str, BaseException] = field(default_factory=dict)
    verify: Callable[[str, float], subprocess.CompletedProcess[str]] | None = None


def _issues_named(f: Fakes, number: int) -> list[dict]:
    """Every issue dict known to ``f`` carrying this number, `issues` and `view` alike.

    A test builds `view` from the same object it put in `issues`, so mutating through one
    reaches both — this also covers the rare case where they are separate objects.
    """
    found = [i for i in f.issues if i.get("number") == number]
    if isinstance(f.view, dict):
        candidates = f.view.values() if "number" not in f.view else [f.view]
        found += [i for i in candidates if i.get("number") == number and i not in found]
    return found


def build_tools(f: Fakes | None = None) -> tuple[FindingsTools, Calls]:
    """A `FindingsTools` answering from `f`, and the record of what it was asked."""
    f = f or Fakes()
    calls = Calls()
    gh_errors = dict(f.gh_errors)

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
                    return f.view[int(argv[2])]
                except KeyError, ValueError:
                    raise AssertionError(argv) from None
            return f.view
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
                issue.setdefault("comments", []).append({"body": body})
        return subprocess.CompletedProcess(list(argv), 0, f"{f.url}\n", "")

    return FindingsTools(gh_json, gh, f.verify or run_verify), calls
