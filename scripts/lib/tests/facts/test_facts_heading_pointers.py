"""The heading pointer: a doc's path in backticks, then the name of one part of it in italics."""

import pytest

from lib.facts.citations import heading_pointers, names_a_part


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "see `docs/landing.md`, *Reading the verdict*",
            [("docs/landing.md", "Reading the verdict")],
        ),
        (
            "`docs/break-glass.md`'s *The age keys* has it",
            [("docs/break-glass.md", "The age keys")],
        ),
        (
            "`docs/landing.md` → *After a PR Merges*",
            [("docs/landing.md", "After a PR Merges")],
        ),
        (
            "the rule (`docs/landing.md`,\n  *A heading that\n  wraps*).",
            [("docs/landing.md", "A heading that\n  wraps")],
        ),
        ("`docs/landing.md` (*Tiers*)", [("docs/landing.md", "Tiers")]),
    ],
)
def test_a_heading_pointer_is_clean(text, expected):
    assert heading_pointers(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "that role's `CLAUDE.md`, *Autonomous-role contract*",
        "`docs/landing.md`, **a bold lead-in**",
        "`docs/landing.md` holds the rule, and *this* is emphasis",
        "```\n`docs/landing.md`, *Reading the verdict*\n```",
        "`scripts/lib/git.py`, *not a doc*",
    ],
)
def test_a_span_that_points_at_no_part_is_flagged_as_no_pointer(text):
    assert heading_pointers(text) == []


_PARTS = """# Landing

## Reading the `VERDICT:` line
The prose mentions a soak window.

- **Every landing waits on promotion.** A lead-in, not a heading.
"""


@pytest.mark.parametrize(
    "part",
    [
        "Reading the `VERDICT:` line",
        "Reading the VERDICT",
        "Every landing waits on promotion",
    ],
)
def test_a_heading_or_a_bold_lead_in_is_a_named_part(part):
    assert names_a_part(part, _PARTS)


@pytest.mark.parametrize("part", ["a soak window", "Reading the tea leaves"])
def test_prose_and_missing_text_name_no_part(part):
    assert not names_a_part(part, _PARTS)
