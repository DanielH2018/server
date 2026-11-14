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
arrive red (#4012). A page under `docs/archive/`, which records the tree as it stood. A
date in an `until <date>` or `as of <date>` clause: those are provenance, and a check
that fires on a passed date fires on every correction the docs carry.

ONE MATCH PER ROW. A pattern that matches nothing fails: the sentence was reworded, and
the row would otherwise pass on nothing. A pattern that matches twice fails too, because
the fixer would rewrite a number the row's author never read.

STATIC PARSING ONLY, as in `fragment_readers.py`: a reader parses its source and never
imports it.

Usage::

    uv run python scripts/docs/doc_values.py   # rewrite, exit 1 if a doc changed
"""

import re
import sys as _sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fragment_readers import module_constant
from lib.cli_help import ArgumentParser
from lib.repo_paths import REPO


@dataclass(frozen=True)
class Value:
    """One sentence that states a value, and where the tree holds that value.

    Attributes:
        doc: repo path of the doc that states the value.
        pattern: a regex with exactly one capture group, the stated value.
        source: repo path of the file the value comes from, named in a failure.
        read: returns the text the doc should state, given the path of `source`.
    """

    doc: str
    pattern: str
    source: str
    read: Callable[[_Path], str]


def _constant(name: str) -> Callable[[_Path], str]:
    return lambda module: str(module_constant(module, name))


_CARDINALS = "zero one two three four five six seven eight nine ten".split()
_ORDINALS = (
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth".split()
)


def staged_paths(script: str) -> list[str]:
    """The paths the docs-refresh cron's one `git add` command names.

    The command may run over several lines that end in a backslash. A comment that
    mentions `git add` is no command.
    """
    lines = [line.strip() for line in script.splitlines()]
    starts = [i for i, line in enumerate(lines) if line.startswith("git add ")]
    assert len(starts) == 1, f"expected one `git add` command, found {len(starts)}"
    words: list[str] = []
    for line in lines[starts[0] :]:
        words += line.removesuffix("\\").split()
        if not line.endswith("\\"):
            break
    return words[2:]


def _staged_count(words: Sequence[str], offset: int = 0) -> Callable[[_Path], str]:
    return lambda script: words[len(staged_paths(script.read_text())) + offset]


_GENERATED_DOCS = ".claude/rules/generated-docs.md"

# source -> (doc, pattern, read) for each sentence that states a value from that source.
_BY_SOURCE: dict[str, list[tuple[str, str, Callable[[_Path], str]]]] = {
    "scripts/diagnostics/probe_lib/health_rollout.py": [
        (
            "docs/deploying.md",
            r"a (\d+)-second restart window",
            _constant("RECENT_RESTART_SECONDS"),
        ),
        (
            "docs/claude-tooling.md",
            r"restarted in the last (\d+)s\.",
            _constant("RECENT_RESTART_SECONDS"),
        ),
    ],
    "hosts/components/docs-refresh/templates/docs-refresh.sh.j2": [
        (
            _GENERATED_DOCS,
            r"^(\w+) paths: `docs/reference/`",
            _staged_count([word.capitalize() for word in _CARDINALS]),
        ),
        (
            _GENERATED_DOCS,
            r"^Adding a (\w+) means editing the cron's `git add`",
            _staged_count(_ORDINALS, offset=1),
        ),
    ],
}

VALUES: tuple[Value, ...] = tuple(
    Value(doc, pattern, source, read)
    for source, rows in _BY_SOURCE.items()
    for doc, pattern, read in rows
)


def _matches(row: Value, text: str) -> list[re.Match[str]]:
    regex = re.compile(row.pattern, re.MULTILINE)
    assert regex.groups == 1, (
        f"pattern must have exactly one capture group: {row.pattern}"
    )
    return list(regex.finditer(text))


def stale(values: tuple[Value, ...] = VALUES, repo: _Path = REPO) -> list[str]:
    """One line per row whose doc fails to state the tree's value once, naming both files."""
    problems = []
    for row in values:
        found = _matches(row, (repo / row.doc).read_text())
        expected = row.read(repo / row.source)
        if not found:
            problem = f"nothing matches /{row.pattern}/; update the row or the sentence"
        elif len(found) > 1:
            problem = f"/{row.pattern}/ matches {len(found)} times; narrow it to one"
        elif found[0].group(1) != expected:
            problem = f"states {found[0].group(1)}, the tree holds {expected}"
        else:
            continue
        problems.append(f"{row.doc} vs {row.source}: {problem}")
    return problems


def fix(values: tuple[Value, ...] = VALUES, repo: _Path = REPO) -> list[str]:
    """Rewrite each stated value that trails the tree, and return the docs rewritten.

    A row whose pattern does not match exactly once is left for `stale` to report.
    """
    rewritten = []
    for row in values:
        path = repo / row.doc
        text = path.read_text()
        found = _matches(row, text)
        expected = row.read(repo / row.source)
        if len(found) != 1 or found[0].group(1) == expected:
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
    ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
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
