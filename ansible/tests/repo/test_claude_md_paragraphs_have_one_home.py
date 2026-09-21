"""No paragraph of the root CLAUDE.md is also, verbatim, in a skill, a rule or a docs page.

The root file loads in every session, so a paragraph there is paid for on every turn. Six of
its sections had grown into copies of a longer skill or docs page (#2128): the same
paragraph, sentence for sentence, in two places, one of which drifts. The shape that holds is
the `## Shell Commands` one — a short summary in root, the detail in one owner — and this
keeps it: a paragraph long enough to be a fact rather than a pointer lives in one file.

Whitespace is normalised so a rewrap is not a difference; a rephrase is, by design — the
guard is against the copy, not the idea.

Run: uv run pytest ansible/tests/repo/test_claude_md_paragraphs_have_one_home.py
"""

import re

from _helpers import REPO, discover_docs

CLAUDE_MD = REPO / "CLAUDE.md"

# Below this a paragraph is a pointer or a heading, and a shared sentence is a convention.
MIN_CHARS = 240


def paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, whitespace-normalised, long enough to be a fact."""
    blocks = re.split(r"\n\s*\n", text)
    normalised = (" ".join(block.split()) for block in blocks)
    return [block for block in normalised if len(block) >= MIN_CHARS]


def copied_paragraphs(root_text: str, others: dict[str, str]) -> list[tuple[str, str]]:
    """(other doc, paragraph) for every root paragraph found verbatim in another doc."""
    found = []
    for name, text in others.items():
        haystack = " ".join(text.split())
        for para in paragraphs(root_text):
            if para in haystack:
                found.append((name, para))
    return found


_FACT = (
    "The lock file is written by the tool and never by hand, because a hand edit fails the "
    "checksum the test recomputes over its units, and the only path back from OUT is the "
    "verify subcommand, which re-reads every atom the section cites and records the hashes."
)


def test_a_paragraph_with_one_home_is_clean():
    assert (
        copied_paragraphs(_FACT, {"docs/x.md": "A different sentence about the lock."})
        == []
    )


def test_a_paragraph_copied_into_a_docs_page_is_flagged():
    rewrapped = _FACT.replace("because a hand edit", "because a hand\n  edit")
    assert copied_paragraphs(_FACT, {"docs/x.md": f"Intro.\n\n{rewrapped}\n"}) == [
        ("docs/x.md", _FACT)
    ]


def test_a_short_pointer_is_not_graded():
    pointer = "The full table is in the `deploy` skill."
    assert copied_paragraphs(pointer, {"docs/x.md": pointer}) == []


# The root file graded 32 paragraphs when this guard landed. A census that finds its subject
# by pattern must know it found something, or a changed split leaves it green over nothing.
MIN_GRADED_PARAGRAPHS = 20


def test_the_census_grades_the_root_file():
    graded = paragraphs(CLAUDE_MD.read_text())
    assert len(graded) >= MIN_GRADED_PARAGRAPHS, (
        f"only {len(graded)} root paragraphs clear MIN_CHARS; the split or the threshold "
        "stopped matching the file"
    )


def test_no_root_paragraph_is_also_in_a_skill_rule_or_docs_page():
    others = {
        str(doc.relative_to(REPO)): doc.read_text(errors="replace")
        for doc in discover_docs()
        if doc != CLAUDE_MD
    }
    assert others, "discover_docs() found no other operator doc; the census is empty"
    copies = copied_paragraphs(CLAUDE_MD.read_text(), others)
    assert not copies, (
        "a root CLAUDE.md paragraph is also verbatim in another doc; shrink the root copy to "
        "a pointer at the owner:\n  "
        + "\n  ".join(f"{name}: {para[:100]}…" for name, para in copies)
    )
