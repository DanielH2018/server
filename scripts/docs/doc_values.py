#!/usr/bin/env python3
"""Rewrite each value a hand-written doc states in a sentence to what the tree holds.

A doc states tunables mid-sentence: "a 180-second restart window". The tunable moves in a
Python constant or a role default, the sentence is not in the diff, and the doc reads
wrong until someone notices. Each row of `VALUES` names one such sentence: the doc, a
regex whose single capture group is the stated value, the file the value comes from, and
a reader that returns the text the doc should state. `stale` compares the two, and `fix`
rewrites the doc.

The `regen-doc-values` prek hook runs this script on a commit that touches a row's doc or
source. It rewrites a stale value and exits 1 like a formatter, so the author stages the
doc, reads the sentence around the value, and commits again. A row the script cannot fix
exits 1 too and names the reason. `scripts/docs/tests/test_doc_values.py` runs `stale` in
CI, for a commit that skipped the hook.

WHAT GETS A ROW. A whole table of tunables is a fragment: `gen_doc_fragments.py`
generates it and the page includes it, so no stated copy exists. A row is for one figure
inside a sentence. The table is hand-maintained. A row is added by the PR that fixes a
value that went stale, because a value has no shape of its own: "14" is a retain count on
one line and a port on the next. A second phrasing of the same value is a second row.

WHAT GETS NONE. A value Renovate bumps, because a bump PR runs no hook and must not
arrive red. A page under `docs/archive/`, which records the tree as it stood. A
date in an `until <date>` or `as of <date>` clause: those are provenance, and a check
that fires on a passed date fires on every correction the docs carry.

ONE MATCH PER ROW. A pattern that matches nothing fails: the sentence was reworded, and
the row would otherwise pass on nothing. A pattern that matches twice fails too, because
the fixer would rewrite a number the row's author never read.

STATIC PARSING ONLY, as in `fragment_readers.py`: a reader parses its source and never
imports it.

`doc_values_lib/` holds the rows and the readers they are built from.

Usage::

    uv run python scripts/docs/doc_values.py   # rewrite, exit 1 if a doc changed
"""

import re
from pathlib import Path as _Path

from docs.doc_values_lib import rows_hosts, rows_roles
from docs.doc_values_lib.readers import Value
from lib.cli_help import ArgumentParser
from lib.repo_paths import REPO

VALUES: tuple[Value, ...] = rows_hosts.VALUES + rows_roles.VALUES


def _matches(row: Value, text: str) -> list[re.Match[str]]:
    regex = re.compile(row.pattern, re.MULTILINE)
    assert regex.groups == 1, (
        f"pattern must have exactly one capture group: {row.pattern}"
    )
    return list(regex.finditer(text))


def _stated(match: re.Match[str]) -> str:
    """The captured value with its line wraps read as spaces, as the doc's reader sees it."""
    return " ".join(match.group(1).split())


def _expected(row: Value, repo: _Path) -> str:
    """The text the row's reader returns, or a `ReaderError` naming why it returned none."""
    try:
        return row.read(repo / row.source)
    except (KeyError, ValueError, AttributeError, TypeError) as error:
        raise ReaderError(f"the reader failed: {error}") from error


class ReaderError(Exception):
    """A row's reader could not find its value in the source."""


def stale(values: tuple[Value, ...] = VALUES, repo: _Path = REPO) -> list[str]:
    """One line per row whose doc fails to state the tree's value once, naming both files."""
    problems = []
    for row in values:
        found = _matches(row, (repo / row.doc).read_text())
        try:
            expected = _expected(row, repo)
        except ReaderError as error:
            problems.append(f"{row.doc} vs {row.source}: {error}")
            continue
        if not found:
            problem = f"nothing matches /{row.pattern}/; update the row or the sentence"
        elif len(found) > 1:
            problem = f"/{row.pattern}/ matches {len(found)} times; narrow it to one"
        elif _stated(found[0]) != expected:
            problem = f"states {found[0].group(1)}, the tree holds {expected}"
        else:
            continue
        problems.append(f"{row.doc} vs {row.source}: {problem}")
    return problems


def fix(values: tuple[Value, ...] = VALUES, repo: _Path = REPO) -> list[str]:
    """Rewrite each stated value that trails the tree, and return the docs rewritten.

    A row whose pattern does not match exactly once, or whose reader fails, is left for
    `stale` to report.
    """
    rewritten = []
    for row in values:
        path = repo / row.doc
        text = path.read_text()
        found = _matches(row, text)
        try:
            expected = _expected(row, repo)
        except ReaderError:
            continue
        if len(found) != 1 or _stated(found[0]) == expected:
            continue
        start, end = found[0].span(1)
        path.write_text(text[:start] + expected + text[end:])
        if row.doc not in rewritten:
            rewritten.append(row.doc)
    return rewritten


def main(
    argv: list[str] | None = None,
    values: tuple[Value, ...] = VALUES,
    repo: _Path = REPO,
) -> int:
    """Entry point: rewrites every stale value and reports what a person must still do.

    Returns:
        0 when every row matched the tree, and 1 when a doc was rewritten or a row needs
        a hand edit.
    """
    ArgumentParser(description=(__doc__ or "").partition("\n")[0]).parse_args(argv)
    rewritten = fix(values, repo)
    problems = stale(values, repo)
    if rewritten:
        print(f"doc_values: rewrote a stated value in: {', '.join(rewritten)}")
        print("  read the sentence around it, stage the doc and commit again")
    if problems:
        print(f"doc_values: {len(problems)} row(s) need a hand edit:")
        for problem in problems:
            print(f"  {problem}")
    if not rewritten and not problems:
        print(f"doc_values: {len(values)} stated value(s) match the tree")
    return 1 if rewritten or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
