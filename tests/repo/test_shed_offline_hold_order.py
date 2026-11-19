"""The shed scenario's offline branch sets `DEPLOY_HOLD` before any resume, by one pinned sentence.

docs/break-glass.md's *one node is down and the survivor runs short of memory* has a bullet for
when GitHub cannot be reached. Its order is a safety rule: a queued `promote` run can start as
soon as GitHub answers, so the hold is set before anything else. Every other rule of that
scenario is a `RunbookRule` row in `tests/repo/test_runbook_rules.py`. This one is not, because
a rule that parses "before", "after", "once" or "until" opened a new bypass with each fix, so
the check pins the one sentence instead and refuses any second statement of the order. The
case table below is the must-refuse and must-pass list that replaced the prose rule.

Run: uv run pytest tests/repo/test_shed_offline_hold_order.py
"""

import re

import pytest
from _runbook_rule import section

from lib.repo_paths import REPO

BREAK_GLASS = REPO / "docs" / "break-glass.md"
SHED_HEADING = "### Scenario: one node is down and the survivor runs short of memory"
OFFLINE_BULLET = "- **When GitHub cannot be reached**"
HOLD_SET = "gh variable set DEPLOY_HOLD"

_RESUME_OR_CLEAR = re.compile(
    r"\bresum\w*|--body none|\"suspend\":false", re.IGNORECASE
)
_ANY_VARIABLE_SET = re.compile(r"\bgh variable set\b")
_STEP_4 = re.compile(r"\bstep 4\b", re.IGNORECASE)
# The offline branch's one ordering sentence, whitespace-normalised. Rewording the bullet
# means rewording this constant in the same commit; see offline_order_problems.
OFFLINE_ORDER_SENTENCE = (
    f"Once the WAN returns, run this step's `{HOLD_SET}` first, before any resume and "
    "before step 4, because a queued `promote` run can start as soon as GitHub answers."
)


def offline_branch(shed: str) -> str:
    """The shed scenario's offline bullet, with its indented continuation lines."""
    lines = shed.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith(OFFLINE_BULLET)),
        None,
    )
    assert start is not None, f"no offline branch starting {OFFLINE_BULLET!r}"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and (
        not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
    ):
        end += 1
    return "\n".join(lines[start:end])


def offline_order_problems(branch: str) -> list[str]:
    """In the offline branch, `DEPLOY_HOLD` is set before any resume or clear.

    - The whitespace-normalised bullet holds OFFLINE_ORDER_SENTENCE exactly once.
    - Outside that sentence, the bullet mentions no resume or clear, no `gh variable set`
      and no step 4. Each would be a second statement of the order that the pin cannot
      rank.

    A reworded bullet fails here until the same commit rewords the pinned sentence, so a
    reviewer reads the new order in the diff. Only `gh variable set DEPLOY_HOLD` sets the
    hold. The pin carries the closing backtick, so a set of another variable, or one with
    `--body none`, does not match it.
    """
    text = " ".join(branch.split())
    count = text.count(OFFLINE_ORDER_SENTENCE)
    if count != 1:
        return [
            f"the offline branch holds the pinned order sentence {count} times, not once: "
            f"{OFFLINE_ORDER_SENTENCE!r}"
        ]
    rest = text.replace(OFFLINE_ORDER_SENTENCE, " ")
    return [
        f"{name} outside the pinned order sentence: {rest[max(0, m.start() - 40) : m.end() + 40]!r}"
        for name, pattern in (
            ("a resume or clear", _RESUME_OR_CLEAR),
            ("a `gh variable set`", _ANY_VARIABLE_SET),
            ("a step 4 mention", _STEP_4),
        )
        for m in pattern.finditer(rest)
    ]


def test_the_offline_branch_sets_the_hold_before_any_resume() -> None:
    shed = section(BREAK_GLASS.read_text(), SHED_HEADING)
    assert offline_order_problems(offline_branch(shed)) == []


# Literal offline bullets, whitespace-normalised. The bullet's opening, which states no order.
_OFFLINE_LEAD = (
    "- **When GitHub cannot be reached**, skip this step and record the reason and the time "
    "in a local file on daniel-box. Step 3 needs only the LAN, so it runs as written."
)
_CANONICAL = (
    "Once the WAN returns, run this step's `gh variable set DEPLOY_HOLD` first, before any "
    "resume and before step 4, because a queued `promote` run can start as soon as GitHub "
    "answers."
)


@pytest.mark.parametrize(
    ("bullet", "refused"),
    [
        pytest.param(f"{_OFFLINE_LEAD} {_CANONICAL}", False, id="canonical-passes"),
        # The pinned sentence reworded or replaced.
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, before step 4 resume each suspended tag, "
            "then run this step's `gh variable set DEPLOY_HOLD`.",
            True,
            id="before-noun-ahead-of-a-resume",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, resume each suspended tag, then after that "
            "run `gh variable set DEPLOY_HOLD`, before step 4.",
            True,
            id="after-that-refers-back-to-the-resume",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, run this step's `gh variable set "
            "DEPLOY_HOLD`. Before that, resume each suspended tag.",
            True,
            id="ordering-word-across-a-sentence-boundary",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, resume each suspended tag first, then run "
            "this step's `gh variable set DEPLOY_HOLD`, before step 4, because a queued "
            "`promote` run can start as soon as GitHub answers.",
            True,
            id="resume-first-wording",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, run this step's `gh variable set "
            "SOMETHING_ELSE` first, before any resume and before step 4, because a queued "
            "`promote` run can start as soon as GitHub answers.",
            True,
            id="set-of-a-different-variable",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Once the WAN returns, run this step's `gh variable set "
            "DEPLOY_HOLD --body none` first, before any resume and before step 4, because a "
            "queued `promote` run can start as soon as GitHub answers.",
            True,
            id="clear-in-the-place-of-the-set",
        ),
        # The pinned sentence intact, with a second statement of the order beside it.
        pytest.param(
            f"{_OFFLINE_LEAD} {_CANONICAL} Before step 4 and before that set, resume each "
            "suspended tag.",
            True,
            id="trailing-step-4-clause-puts-the-resume-first",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} Resume each suspended tag. {_CANONICAL}",
            True,
            id="resume-sentence-ahead-of-the-pin",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} {_CANONICAL} Then run `gh variable set DEPLOY_HOLD --body none`.",
            True,
            id="clear-after-the-pin",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} {_CANONICAL} Then run `gh variable set SOMETHING_ELSE --body x`.",
            True,
            id="other-variable-set-beside-the-pin",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} {_CANONICAL} Then go to step 4.",
            True,
            id="bare-step-4-beside-the-pin",
        ),
        pytest.param(
            f"{_OFFLINE_LEAD} {_CANONICAL} {_CANONICAL}",
            True,
            id="pin-stated-twice",
        ),
    ],
)
def test_the_offline_order_case_table(bullet: str, refused: bool) -> None:
    problems = offline_order_problems(bullet)
    assert bool(problems) is refused, problems
