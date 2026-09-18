"""The deploy skill's exit table lists exactly the codes `deploy.sh` can exit with.

`scripts/deploy_tools/exit_codes.py` is the one source for `deploy.sh`'s contract in code, and
`auto-mode-bridge.py`'s `_DEPLOY_EXITS` is set-equality tested against it. The table in
`.claude/skills/deploy/SKILL.md` is the copy an operator reads when a bare `Exit code N`
arrives, and until this file nothing read it: 77 and 78 each landed as a new row by hand, and
the sentence above the table counted "seven" no-verdict codes while the table held nine rows,
because 64 (bad flags) also deploys nothing and belongs to neither count.

`docs/deploying.md` carries a second table, headed "resume points" and so without 20; it had
lost 76 by the time this guard was written.

The remedy column is prose and stays unguarded. What is checkable is the code set, and that
the docs name `DEPLOY_SH_NO_VERDICT` rather than a numeral that rots on the next code.
"""

import re

import pytest
from deploy_tools import exit_codes
from deploy_tools.exit_codes import DEPLOY_OK, DEPLOY_SH_NO_VERDICT

from _helpers import REPO

DEPLOY_SKILL = REPO / ".claude" / "skills" / "deploy" / "SKILL.md"
DEPLOYING_DOC = REPO / "docs" / "deploying.md"
CLAUDE_MD = REPO / "CLAUDE.md"

# Only the wrapper's own contract; the module also names the tick's, await_ci's, land.sh's,
# the staging gate's and publish_pr's, none of which the deploy skill tabulates.
DEPLOY_SH_CODES = frozenset(
    value
    for name, value in vars(exit_codes).items()
    if name.startswith("DEPLOY_") and isinstance(value, int) and value != DEPLOY_OK
)

# The table cannot lose rows to a rename of the module's constants, so the members are
# named here as well: a census that reads its own subject by pattern must know what it
# expects to find.
KNOWN_CODES = frozenset({2, 3, 4, 20, 64, 75, 76, 77, 78})

_TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)


def _table_codes(text):
    """Every numeric first column of a Markdown table row in `text`."""
    return frozenset(int(code) for code in _TABLE_ROW.findall(text))


def test_the_module_still_defines_the_codes_the_table_is_measured_against():
    assert DEPLOY_SH_CODES == KNOWN_CODES, (
        "deploy.sh gained or lost an exit code; update KNOWN_CODES, the skill's table, and "
        "auto-mode-bridge's _DEPLOY_EXITS together"
    )


# docs/deploying.md's table is headed "resume points" and leaves 20 to the skill on purpose;
# it had lost 76 by the time this guard was written, which is the drift it now catches.
@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (DEPLOY_SKILL, DEPLOY_SH_CODES),
        (DEPLOYING_DOC, DEPLOY_SH_CODES - {exit_codes.DEPLOY_PLAYBOOK_FAILED}),
    ],
    ids=lambda arg: arg.name if hasattr(arg, "name") else "",
)
def test_each_exit_table_lists_exactly_the_codes_deploy_sh_exits_with(doc, expected):
    found = _table_codes(doc.read_text())
    assert found == expected, (
        f"{doc.name}'s exit table is out of step with exit_codes.py: "
        f"missing {sorted(expected - found)}, undefined {sorted(found - expected)}"
    )


@pytest.mark.parametrize(
    "doc", [DEPLOY_SKILL, DEPLOYING_DOC, CLAUDE_MD], ids=lambda p: p.name
)
def test_the_operator_docs_name_the_no_verdict_constant_rather_than_counting_it(doc):
    """A count of the no-verdict codes was wrong in both files within two added codes.

    Naming the frozenset sends a reader to the thing that is true by construction; a numeral
    is true until the next `exit_codes.py` change, which nothing here can see.
    """
    text = doc.read_text()
    assert "DEPLOY_SH_NO_VERDICT" in text, (
        f"{doc.name} must name DEPLOY_SH_NO_VERDICT for the codes that deployed nothing"
    )
    assert not re.search(
        r"\b(Seven|Eight|Nine) of (them|its non-zero exits)\b", text
    ), f"{doc.name} counts the no-verdict exit codes in prose again; name the constant"


def test_the_no_verdict_set_is_the_table_minus_the_two_that_are_not_resume_points():
    """The classification the docs make: every code but 64 and 20 is a retry-safe refusal."""
    assert DEPLOY_SH_CODES - DEPLOY_SH_NO_VERDICT == {
        exit_codes.DEPLOY_BAD_FLAGS,
        exit_codes.DEPLOY_PLAYBOOK_FAILED,
    }


# -- the parser's own red proof -----------------------------------------------------------


def test_a_table_missing_a_row_is_flagged():
    table = "| Exit | Meaning |\n|---|---|\n| 75 | busy |\n| 20 | failed |\n"
    assert _table_codes(table) == {75, 20}
    assert _table_codes(table) != DEPLOY_SH_CODES


def test_a_numeral_outside_the_first_column_is_not_a_code():
    prose = "Exit 3 is `--changed`'s answer after 7200s under ADR-0017.\n| Exit | 3 |\n"
    assert _table_codes(prose) == frozenset(), (
        "only a numeric FIRST column is a table row"
    )
