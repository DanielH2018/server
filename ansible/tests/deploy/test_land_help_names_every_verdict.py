"""`land.sh --help` lists exactly the `Verdict` members it can print.

`land_lib/outcome.py`'s `Verdict` is the closed vocabulary the `VERDICT:` line prints, and the
Landings board groups by it. `land.py`'s module docstring is what `--help` prints, and it is
what an operator at a terminal reads to decode the line.

This module also pinned the paragraph in `.claude/skills/land-after-merge/SKILL.md` that
enumerated the same members, until 2026-09-30. That paragraph is gone: the skill now points at
`--help` and at `land_lib/outcome.py` rather than restating them, which is the copy this guard
existed to catch drifting (issue #2853). The `--help` half stays, because it IS the copy an
operator reads.
"""

import ast

from deploy_tools.land_lib.outcome import VERDICTS

from _helpers import REPO

# land.py's module docstring is what `land.sh --help` prints; its verdict line is the copy an
# operator at a terminal reads, pipe-separated rather than backticked.
LAND_PY = REPO / "scripts" / "deploy_tools" / "land.py"
_HELP_VERDICT_LINE = "Verdicts printed on stdout:"

EXPECTED = frozenset(str(v) for v in VERDICTS)


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


def test_the_enum_still_has_the_members_the_help_line_is_measured_against():
    assert {"settled", "deploy-failed", "tip-outran-retries"} <= EXPECTED, (
        "Verdict lost a member this test names; the check above is measured against a "
        "vocabulary that no longer exists"
    )


# -- the parser's own red proof -----------------------------------------------------------


def test_a_help_line_naming_a_verdict_the_enum_lacks_is_flagged():
    docstring = "Usage.\n\nVerdicts printed on stdout: settled | ci-purple.\n\nMore."
    found = _help_verdicts(docstring)
    assert found == {"settled", "ci-purple"}
    assert found != EXPECTED


def test_a_help_line_missing_a_verdict_is_flagged():
    """The other direction: a member dropped from the line must fail, not pass quietly."""
    listing = " | ".join(sorted(EXPECTED - {"settled"}))
    found = _help_verdicts(f"Usage.\n\n{_HELP_VERDICT_LINE} {listing}.\n\nMore.")
    assert found != EXPECTED and "settled" not in found
