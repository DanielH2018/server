"""The root CLAUDE.md points at the check that holds a fact; it does not restate the fact and
then disclaim the restatement.

Until #2060 the file carried lines of the shape "read it rather than trusting this one" and
"derive the set with grep rather than trusting a count written here" — prose admitting it could
not hold the fact it had just written out. Each such fact already had an owner (a hook, a
generator, a test), so the fix was to cut the restatement to a pointer. This keeps the shape
from coming back: a sentence that needs the disclaimer is a sentence that should have been a
pointer, and every session loads this file.

Run: uv run pytest ansible/tests/repo/test_claude_md_has_no_self_disclaiming_prose.py
"""

import re

from _helpers import REPO

CLAUDE_MD = REPO / "CLAUDE.md"

# The three phrasings the file used; a new one belongs here, not in the doc.
SELF_DISCLAIMER = re.compile(
    r"rather than trusting|written here went stale|read it rather", re.IGNORECASE
)


def self_disclaiming_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if SELF_DISCLAIMER.search(line)]


def test_a_pointer_is_clean():
    assert (
        self_disclaiming_lines(
            "`FRAGMENTS` in that generator lists every tunable it reads."
        )
        == []
    )


def test_a_restatement_that_disclaims_itself_is_flagged():
    line = "`FRAGMENTS` in the generator is the current list; read it rather than trusting this one."
    assert self_disclaiming_lines(line) == [line]


def test_the_root_claude_md_disclaims_none_of_its_own_lines():
    offenders = self_disclaiming_lines(CLAUDE_MD.read_text())
    assert not offenders, (
        "CLAUDE.md restates a fact and then says not to trust the restatement; cut the "
        "restatement to a pointer at the check that holds it:\n  "
        + "\n  ".join(offenders)
    )
