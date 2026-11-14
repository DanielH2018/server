"""The values a hand-written doc states in a sentence, each re-derived from the tree.

A doc states tunables mid-sentence: "a 180-second restart window". The tunable moves in a
Python constant or a role default, the sentence is not in the diff, and the doc reads
wrong until someone notices. Each row of `VALUES` names one such sentence: the doc, a
regex whose single capture group is the stated value, the file the value comes from, and
a reader that returns what that file holds. `stale` compares the two.

WHAT GETS A ROW. A whole table of tunables is a fragment: `gen_doc_fragments.py`
generates it and the page includes it, so no stated copy exists. A row is for one figure
inside a sentence. The table is hand-maintained. A row is added by the PR that fixes a
value that went stale, because a value has no shape of its own: "14" is a retain count on
one line and a port on the next. A second phrasing of the same value is a second row.

WHAT GETS NONE. A value Renovate bumps, because a bump PR runs no hook and must not
arrive red (#4012). A page under `docs/archive/`, which records the tree as it stood. A
date in an `until <date>` or `as of <date>` clause: those are provenance, and a check
that fires on a passed date fires on every correction the docs carry.

STATIC PARSING ONLY, as in `fragment_readers.py`: a reader parses its source and never
imports it.
"""

import re
import sys as _sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fragment_readers import module_constant
from lib.repo_paths import REPO


@dataclass(frozen=True)
class Value:
    """One sentence that states a value, and where the tree holds that value.

    Attributes:
        doc: repo path of the doc that states the value.
        pattern: a regex with exactly one capture group, the stated value.
        source: repo path of the file `read` parses, named in a failure.
        read: returns the value the tree holds, given the repo root.
    """

    doc: str
    pattern: str
    source: str
    read: Callable[[_Path], str]


def _constant(source: str, name: str) -> Callable[[_Path], str]:
    return lambda repo: str(module_constant(repo / source, name))


_HEALTH_ROLLOUT = "scripts/diagnostics/probe_lib/health_rollout.py"

VALUES: tuple[Value, ...] = (
    Value(
        "docs/deploying.md",
        r"a (\d+)-second restart window",
        _HEALTH_ROLLOUT,
        _constant(_HEALTH_ROLLOUT, "RECENT_RESTART_SECONDS"),
    ),
    Value(
        "docs/claude-tooling.md",
        r"restarted in the last (\d+)s\.",
        _HEALTH_ROLLOUT,
        _constant(_HEALTH_ROLLOUT, "RECENT_RESTART_SECONDS"),
    ),
)


def mismatches(text: str, pattern: str, expected: str) -> list[str]:
    """Every way `text` fails to state `expected` at `pattern`. Empty means clean.

    A pattern that matches nowhere is a failure: the sentence was reworded, and the row
    would otherwise pass on nothing.
    """
    regex = re.compile(pattern, re.MULTILINE)
    assert regex.groups == 1, f"pattern must have exactly one capture group: {pattern}"
    found = regex.findall(text)
    if not found:
        return [f"nothing matches /{pattern}/; update the row or the sentence"]
    return [
        f"states {value}, the tree holds {expected}"
        for value in found
        if value != expected
    ]


def stale(values: tuple[Value, ...] = VALUES, repo: _Path = REPO) -> list[str]:
    """One line per row whose doc and source disagree, naming both files."""
    problems = []
    for row in values:
        text = (repo / row.doc).read_text()
        for problem in mismatches(text, row.pattern, row.read(repo)):
            problems.append(f"{row.doc} vs {row.source}: {problem}")
    return problems
