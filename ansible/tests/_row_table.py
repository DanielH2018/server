"""The row harness both property tables share: one verdict, and the textual census row type.

A policy-shaped guard is a selector, a predicate and a floor that stops the census passing over
nothing. Written as its own file, each guard restated the walk, the floor, the offender report
and the red-proof pair. As a row, the selector and the predicate are the row's own code and
everything else is here once (#3384, #3407).

Two tables use it:

- `ansible/tests/k8s/_property_table.py` selects from the rendered k8s manifests. Its `check`
  ends in `verdict` below.
- `Census` below selects from the tracked tree: `git ls-files` or `role_dirs()`. Its rows live
  in `ansible/tests/repo/test_census_rows_*.py`, one file per domain, because
  `TEST_CAP` in `_ratchet.py` caps any one module.

What a row buys over a file:

- **The subject leaving fails the row.** A selector that matches nothing reports
  `subject gone: delete this row` rather than passing over an empty census. That is the
  retirement rule in `.claude/rules/python-layout.md`, enforced instead of remembered.
- **The floor and the named members are data.** `min_matches` catches a census that shrank.
  `must_find` names the members a row exists for, so the failure says which one went.
  A row whose subject is a match inside a file rather than the file sets `count`, and then
  the floor counts matches and only a file holding one is a member.
- **The red proof is mandatory.** A row cannot be built without `red` (subjects the predicate
  must flag) and `green` (subjects it must pass), and each table runs both for every row.
- **An exemption carries its reason, and a stale one fails.** `allow` maps a key to why it is
  exempt. A key that no longer matches, or no longer offends, is reported for removal.

Run: uv run pytest ansible/tests/repo/test_row_table.py
"""

import ast
import gc
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType

from _helpers import REPO
from lib.proc_testing import run


def verdict(
    name: str,
    reason: str,
    found: Mapping[str, int],
    offenders: Iterable[tuple[str, str]],
    *,
    min_matches: int,
    must_find: frozenset[str],
    allow: Mapping[str, str],
) -> list[str]:
    """Every problem a row has, or [] when it holds.

    `found` counts the matches per key. `offenders` pairs each offending key with the report
    line for it; an allowed key's lines are dropped here, not by the caller.
    """
    matches = sum(found.values())
    if not matches:
        return [f"{name}: subject gone: delete this row (the selector matched nothing)"]

    offenders = list(offenders)
    problems = []
    if matches < min_matches:
        problems.append(
            f"{name}: {matches} matches, floor is {min_matches} — coverage shrank, "
            "or the selector broke"
        )
    if missing := sorted(must_find - found.keys()):
        problems.append(f"{name}: never matched {missing}")
    for key in sorted(allow.keys() - {key for key, _ in offenders}):
        problems.append(
            f"{name}: allow entry {key!r} is stale (no longer matched or no longer "
            "offending) — remove it"
        )
    problems += [f"{name}: {line}" for key, line in offenders if key not in allow]
    if problems:
        problems.append(f"  why the property holds: {reason}")
    return problems


# One tree per source text for the whole process. Every row of a census reads the same modules,
# and parsing was 5.15s of the 9.23s one row took over 1620 of them (#6437), so the rows of
# `scripts/tests/test_census_rows_test_*.py` share the parse rather than each paying it again.
# Holding every tree costs memory: the two census files' maxrss went from 102 MiB to 643 MiB
# (measured 2026-10-29, 1624 modules). Every row sweeps the whole census, so under
# `--dist loadscope` each worker that runs either file holds a full copy, not a share.
# Unbounded on purpose: every row sweeps the census in the same order, so an LRU smaller than
# the census never hits. A caller only reads the tree; one that edited it would edit every row's.
#
# DECIDED: freeze the collector's view after each parse. Millions of retained nodes made every
# full collection rescan them: the cache alone cut the two census files from 18.1-18.4s to
# 14.8-16.3s in-process, with GC time up from 1.1s to 3.4-3.9s. Frozen, they ran in
# 11.5-12.1s with 0.14s of GC (interleaved runs, load 3-5, 2026-10-29). The cost is that a
# reference cycle already unreachable at a freeze is never freed, which a test process outlives.
@cache
def parse(source: str) -> ast.Module:
    """The AST of `source`, parsed once per process however many rows ask for it."""
    tree = ast.parse(source)
    gc.freeze()
    return tree


def drop_parsed() -> None:
    """Release every tree `parse` holds, for a census file to call once its last row has run.

    It bounds how long a worker holds the trees, not how many: a worker running both census
    files back to back parses the census once per file. Each tree is acyclic, so dropping the
    cache frees it by reference count. `gc.unfreeze` hands the frozen objects back to the
    collector, so a cycle frozen with them can be collected again.
    """
    parse.cache_clear()
    gc.unfreeze()


# ── The textual census row ────────────────────────────────────────────────────────────


class Subject:
    """One tracked file: its repo-relative path, and its text read on first use.

    A row that judges the path alone never reads the file. A fixture passes `text` in.
    """

    def __init__(self, rel: str, text: str | None = None):
        self.rel = rel
        self._text = text

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = (REPO / self.rel).read_text(errors="replace")
        return self._text


# The repo-relative paths a row censuses, or the subjects themselves when a subject is not one
# file (a family of sibling modules, say).
Files = Callable[[], Iterable[str | Subject]]

# One entry per hit, [] when the subject holds. An entry is what the report prints after the
# path: a line number, a quoted line, a call name.
Offence = Callable[[Subject], list[str]]


def lines_matching(pattern: re.Pattern[str]) -> Offence:
    """An offence reporting every line `pattern` matches, by number."""

    def offence(subject: Subject) -> list[str]:
        return [
            f"line {n}: {line.strip()}"
            for n, line in enumerate(subject.text.splitlines(), 1)
            if pattern.search(line)
        ]

    return offence


def text_lacks(needle: str, what: str) -> Offence:
    """An offence for the positive half of a pair: the file must still contain `needle`."""

    def offence(subject: Subject) -> list[str]:
        return [] if needle in subject.text else [f"no longer {what}"]

    return offence


@dataclass(frozen=True)
class Census:
    name: str
    reason: str
    files: Files
    offence: Offence
    # Every `red` subject must be flagged and every `green` one must pass.
    red: tuple[Subject, ...]
    green: tuple[Subject, ...]
    min_matches: int = 1
    must_find: frozenset[str] = frozenset()
    allow: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # How many matches a file holds, for a row whose subject is a match inside the file (a
    # ship line in a tasks file). None counts every selected file once. A file holding no
    # match is not a member, so `must_find` names files that still hold one.
    count: Callable[[Subject], int] | None = None


@cache
def _ls_files(pathspecs: tuple[str, ...]) -> tuple[str, ...]:
    listed = run(["git", "ls-files", "-z", "--", *pathspecs], cwd=REPO, check=True)
    return tuple(rel for rel in listed.stdout.split("\0") if rel)


def tracked(*pathspecs: str) -> list[str]:
    """Every tracked path matching a git pathspec, sorted. `*` crosses `/` in a pathspec.

    `git ls-files` rather than a glob: a glob walks whatever is on disk, which includes the
    sibling checkouts under `.claude/worktrees/` and a retired role's `__pycache__` shell.
    """
    return sorted(_ls_files(pathspecs))


def check(row: Census, files: Iterable[str | Subject] | None = None) -> list[str]:
    """Every problem `row` has over the tree, or over `files` when a test passes them in."""
    found: dict[str, int] = {}
    offenders = []
    for item in row.files() if files is None else files:
        subject = item if isinstance(item, Subject) else Subject(item)
        if hits := 1 if row.count is None else row.count(subject):
            found[subject.rel] = hits
        offenders += [
            (subject.rel, f"{subject.rel}: {hit}") for hit in row.offence(subject)
        ]
    return verdict(
        row.name,
        row.reason,
        found,
        offenders,
        min_matches=row.min_matches,
        must_find=row.must_find,
        allow=row.allow,
    )


def proof_problems(row: Census) -> list[str]:
    """The red/green pair: a red subject the row passes, or a green one it flags."""
    problems = [f"red {s.rel!r} passed" for s in row.red if not row.offence(s)]
    problems += [
        f"green {s.rel!r} flagged: {hits}"
        for s in row.green
        if (hits := row.offence(s))
    ]
    if not row.red or not row.green:
        problems.append("a row needs at least one red and one green subject")
    return problems
