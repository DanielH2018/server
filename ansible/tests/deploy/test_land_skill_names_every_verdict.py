"""The land-after-merge skill's `VERDICT:` paragraph lists exactly the `Verdict` members.

`land_lib/outcome.py`'s `Verdict` is the closed vocabulary the `VERDICT:` line prints, and the
Landings board groups by it. The paragraph in `.claude/skills/land-after-merge/SKILL.md` that
enumerates them is what an operator reads to decode the line, and nothing read it: the issue
that filed this counted 15 members against an enum of 14.

The paragraph groups its members by meaning ("the four give-ups"); that grouping is
commentary and stays unguarded. Membership is what this pins, in both directions.
"""

import ast
import re

from deploy_tools.land_lib.outcome import VERDICTS

from _helpers import REPO

LAND_SKILL = REPO / ".claude" / "skills" / "land-after-merge" / "SKILL.md"
# land.py's module docstring is what `land.sh --help` prints; its verdict line is the copy an
# operator at a terminal reads, pipe-separated rather than backticked.
LAND_PY = REPO / "scripts" / "deploy_tools" / "land.py"
_HELP_VERDICT_LINE = "Verdicts printed on stdout:"

# A backticked token shaped like a verdict: lowercase words joined by hyphens, nothing else.
# `land.sh` (a dot) and `VERDICT:` (a colon) share the paragraph and are not verdicts.
_VERDICT_SHAPED = re.compile(r"`([a-z]+(?:-[a-z]+)*)`")

EXPECTED = frozenset(str(v) for v in VERDICTS)


def _listing_paragraph(text):
    """The blank-line-delimited paragraph that names the most `Verdict` members.

    Located from the enum rather than from the prose around it, so a reword of the paragraph
    cannot move the test onto a different one.
    """
    return max(
        text.split("\n\n"),
        key=lambda paragraph: len(EXPECTED & set(_VERDICT_SHAPED.findall(paragraph))),
    )


def _verdict_tokens(paragraph):
    return frozenset(_VERDICT_SHAPED.findall(paragraph))


def test_the_land_skill_lists_exactly_the_verdicts_the_enum_defines():
    found = _verdict_tokens(_listing_paragraph(LAND_SKILL.read_text()))
    assert found == EXPECTED, (
        f"land-after-merge/SKILL.md's VERDICT paragraph is out of step with Verdict: "
        f"missing {sorted(EXPECTED - found)}, not in the enum {sorted(found - EXPECTED)}"
    )


def _help_verdicts(docstring):
    """The pipe-separated tokens after `Verdicts printed on stdout:` in land.py's docstring."""
    paragraphs = [
        p for p in docstring.split("\n\n") if p.startswith(_HELP_VERDICT_LINE)
    ]
    assert len(paragraphs) == 1, (
        f"land.py --help no longer has one {_HELP_VERDICT_LINE!r} paragraph"
    )
    listing = paragraphs[0].removeprefix(_HELP_VERDICT_LINE).rstrip(".")
    return frozenset(token.strip() for token in listing.split("|"))


def test_land_help_lists_exactly_the_verdicts_the_enum_defines():
    found = _help_verdicts(ast.get_docstring(ast.parse(LAND_PY.read_text())))
    assert found == EXPECTED, (
        f"land.py's --help verdict line is out of step with Verdict: "
        f"missing {sorted(EXPECTED - found)}, not in the enum {sorted(found - EXPECTED)}"
    )


def test_the_enum_still_has_the_members_the_prose_is_measured_against():
    assert {"settled", "deploy-failed", "tip-outran-retries"} <= EXPECTED, (
        "Verdict lost a member this test names; the paragraph check above is measured "
        "against a vocabulary that no longer exists"
    )


# -- the parser's own red proof -----------------------------------------------------------


def test_a_paragraph_naming_a_verdict_the_enum_lacks_is_flagged():
    prose = "prints `settled`, `unhealthy` or `ci-purple`."
    found = _verdict_tokens(prose)
    assert "ci-purple" in found
    assert found != EXPECTED


def test_a_dotted_or_capitalised_token_is_not_verdict_shaped():
    assert (
        _verdict_tokens("`land.sh` prints a `VERDICT:` line; `--tags` too")
        == frozenset()
    )


def test_a_help_line_naming_a_verdict_the_enum_lacks_is_flagged():
    docstring = "Usage.\n\nVerdicts printed on stdout: settled | ci-purple.\n\nMore."
    found = _help_verdicts(docstring)
    assert found == {"settled", "ci-purple"}
    assert found != EXPECTED


def test_the_locator_picks_the_paragraph_with_the_most_members():
    text = "one `settled` here.\n\n`settled`, `unhealthy` and `blocked` there.\n\nnone."
    assert _listing_paragraph(text).startswith("`settled`, `unhealthy`")
