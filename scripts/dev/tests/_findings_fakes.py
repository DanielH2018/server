"""Fakes for findings.py's three boundaries: canned gh answers keyed by argv, calls recorded.

`gh_json` dispatches on the first two argv elements — `issue list`, `label list`,
`issue view` — so a test names the answer it wants without restating the argv that
`load_issues` and `_existing_labels` assemble. That is the point of keying it this way:
both of those functions were monkeypatched wholesale until 2026-09-04, so no test ever ran
the argv they build or the label planning that reads their result. An argv pair no fake
answers is an AssertionError rather than an empty list, because a silent `[]` reads as "the
register is empty" and would pass most of these assertions.

`Calls` records BOTH boundaries. Several tests exist to prove nothing was written, and the
label read is a `gh_json` call — recording only `gh` would let them pass while gh ran.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/dev

import findings
from dev.findings_tools import FindingsTools, run_verify

CREATED_URL = "https://github.com/o/r/issues/42"


@dataclass
class Calls:
    """Every boundary call in order: `gh` argv as lists, `gh_json` argv as tuples."""

    gh: list[list[str]] = field(default_factory=list)
    gh_json: list[tuple[str, ...]] = field(default_factory=list)

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
    url: str = CREATED_URL
    # Consumed one per `gh` call, in order; a call past the end succeeds.
    gh_errors: list[BaseException] = field(default_factory=list)
    # Keyed by the same `issue list` / `label list` / `issue view` pair `gh_json` dispatches
    # on, so a test can fail the issue read while the label read still answers.
    json_errors: dict[str, BaseException] = field(default_factory=dict)
    verify: Callable[[str, float], subprocess.CompletedProcess[str]] | None = None


def build_tools(f: Fakes | None = None) -> tuple[FindingsTools, Calls]:
    """A `FindingsTools` answering from `f`, and the record of what it was asked."""
    f = f or Fakes()
    calls = Calls()
    errors = iter(f.gh_errors)

    def gh_json(*argv, **kwargs):
        calls.gh_json.append(argv)
        key = " ".join(argv[:2])
        if key in f.json_errors:
            raise f.json_errors[key]
        if key == "label list":
            names = findings.LABELS if f.labels is None else f.labels
            return [{"name": name} for name in names]
        if key == "issue view":
            return f.view
        if key == "issue list":
            return list(f.issues)
        raise AssertionError(argv)

    def gh(*argv, **kwargs):
        calls.gh.append(list(argv))
        error = next(errors, None)
        if error is not None:
            raise error
        return subprocess.CompletedProcess(list(argv), 0, f"{f.url}\n", "")

    return FindingsTools(gh_json, gh, f.verify or run_verify), calls
