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
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import cache, partial
from pathlib import Path
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


# ── Rows that share one census's parse ───────────────────────────────────────────────
#
# Every row of `scripts/tests/test_census_rows_test_*.py` reads the same modules, and parsing
# was 5.15s of the 9.23s one row took over 1620 of them (#6437). `share` makes those rows judge
# a module together. The first shared row to ask about a module runs every shared row's offence
# on it from one tree, keeps only their hits, and drops the tree.
#
# DECIDED: keep each module's hits, not its tree. Holding every tree cut the two census files
# from 11.7s to 8.4s but held 618 MiB per process (measured 2026-10-29, 1624 modules), and
# dropping the trees did not lower RSS: pymalloc keeps the freed arena pages resident (#6465).
# An LRU of trees never hits, because each row sweeps the census in the same order. The hits
# are a few strings per module, and PR #6464 records the measurements.


def _digest(rel: str, text: str) -> tuple[str, bytes]:
    return rel, hashlib.blake2b(text.encode(), digest_size=16).digest()


@dataclass
class Sharing:
    """One process's shared rows, the hits they found, and the trees still to be read.

    The census files use the one instance below. A test builds its own, so its toy rows never
    run over the real census.
    """

    # Each shared row's own offence, by row name.
    offences: dict[str, Offence] = field(default_factory=dict)
    selectors: list[Files] = field(default_factory=list)
    census: frozenset[str] | None = None
    # A subject's hits per shared row, keyed on its path and a digest of its text: two
    # fixtures can share a path, and the key need not hold every census module's text alive.
    hits: dict[tuple[str, bytes], dict[str, tuple[str, ...] | Exception]] = field(
        default_factory=dict
    )
    # The trees `parse` hands out instead of reparsing. A module is here while its hits are
    # being found, or from when another module's import reads it until its own hits are found.
    held: dict[str, ast.Module | None] = field(default_factory=dict)

    def share(self, rows: Iterable[Census]) -> tuple[Census, ...]:
        """`rows`, each judging a subject through the hits every shared row finds in one pass.

        Call it on a census file's rows at import. A row's offence still runs on its own `red`
        and `green` subjects, through the same pass the census takes.
        """
        rows = tuple(rows)
        self.census = None
        for row in rows:
            self.offences[row.name] = row.offence
            if row.files not in self.selectors:
                self.selectors.append(row.files)
        return tuple(
            replace(row, offence=partial(self._hits, row.name)) for row in rows
        )

    def _hits(self, name: str, subject: Subject) -> list[str]:
        text = subject.text
        hits = self.hits.setdefault(_digest(subject.rel, text), {})
        if missing := [other for other in self.offences if other not in hits]:
            self.held.setdefault(text, None)
            try:
                for other in missing:
                    try:
                        hits[other] = tuple(self.offences[other](subject))
                    # A red subject for a textual row need not parse; only that row's own
                    # verdict may fail on what its offence raised.
                    except Exception as error:
                        hits[other] = error
            finally:
                self.held.pop(text, None)
        if isinstance(found := hits[name], Exception):
            raise found
        return list(found)

    def _census(self) -> frozenset[str]:
        if self.census is None:
            self.census = frozenset(
                item.rel if isinstance(item, Subject) else item
                for files in self.selectors
                for item in files()
            )
        return self.census

    def parse(self, source: str) -> ast.Module:
        """The AST of `source`: the tree held for it while a shared pass needs it, else a new one."""
        tree = self.held.get(source)
        if tree is None:
            tree = ast.parse(source)
            if source in self.held:
                self.held[source] = tree
        return tree

    def parse_ahead(self, path: Path, source: str) -> ast.Module:
        """`parse` for a module read because another imports it.

        A shared census module whose own hits are not found yet keeps the tree until they are,
        so reading it early does not cost it a second parse.
        """
        tree = self.parse(source)
        if path.is_relative_to(REPO):
            rel = path.relative_to(REPO).as_posix()
            if _digest(rel, source) not in self.hits and rel in self._census():
                self.held[source] = tree
        return tree


_SHARING = Sharing()
share = _SHARING.share
parse = _SHARING.parse
parse_ahead = _SHARING.parse_ahead
