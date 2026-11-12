"""The fix phase and the delta review that judges it, run once or twice (#7687).

Of 13 `needs-input` batches between 2026-10-25 and 2026-11-08 that ended with findings left
after the single fix round, 12 left a real defect. A second round runs only when the first
delta review leaves a finding of medium severity or worse (`review_record.survivors`); a
red/green batch whose green gate failed before the fix keeps one round, because `green_contest`
judges the gate's finding from the first fix's reply.
"""

from functools import partial
from typing import TYPE_CHECKING

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.review.delta_confirm import split_confirmed
from fanout_lib.review.green_contest import contested, settle
from fanout_lib.review.red_gate import GREEN_FILE, Gate
from fanout_lib.review.review_prompts import delta_prompt, fix_prompt
from fanout_lib.review.review_record import Phase, actionable, findings_of, survivors
from fanout_lib.status import PR_URL

if TYPE_CHECKING:
    from fanout_lib.review.review import Pipeline

MAX_FIX_ROUNDS = 2


def fix_rounds(
    pipe: "Pipeline",
    issues: str,
    base: str,
    head: str,
    red: tuple[str, Gate] | None,
    green: str,
    last: Phase,
) -> tuple[Phase, str]:
    """Fix `record.actionable`, review the fix, and fix again what survived it.

    Args:
        red: the red phase's (SHA, gate), None for a batch without one.
        green: the green gate's refusal before the fix, "" when it passed.
        last: the phase whose report names the PR until a fix report does.

    Returns the last phase whose report names the PR, and the green gate's refusal after the
    rounds. Sets `record.remaining`, `record.unverified` and `record.fix_rounds`.
    """
    record = pipe.record
    asked = record.actionable
    for number in range(1, MAX_FIX_ROUNDS + 1):
        fix = pipe._claude(
            "fix",
            pipe._resume(),
            fix_prompt(asked, record.pr, red[0] if red else ""),
        )
        record.fix_rounds = number
        last = fix if PR_URL.search(fix.text) else last
        after = record.head = pipe._git("rev-parse", "HEAD")
        judged = False
        # A second round that cannot tell what it resolved leaves the first round's leftovers.
        unresolved = list(record.remaining if number > 1 else asked)
        # A contest of a correct fix commits nothing, and still needs its fresh reader.
        if after == head and not contested(green, fix.text, red[1] if red else None):
            record.remaining = unresolved
        else:
            delta = pipe._claude(
                "review-delta",
                pipe._reviewer(),
                delta_prompt(issues, base, head, after, asked, fix.text),
            )
            left, delta_error = findings_of(delta)
            # A failed delta review proves nothing was resolved.
            if left is None:
                record.remaining = unresolved
            else:
                # A reported finding is left only if the fix head's file holds the line it
                # quotes (#6547); the rest are carried unverified and hold nothing.
                record.remaining, record.unverified = split_confirmed(
                    actionable(left), partial(pipe._read_at, after)
                )
                judged = True
            if delta_error:
                record.review_error = f"delta review: {delta_error}"
        asked = survivors(record.remaining)
        if not (judged and asked and not green and number < MAX_FIX_ROUNDS):
            break
        head = after
    # The fix may touch the red tests even when the first run passed, so the PR that ships is
    # the one the gate reads.
    if red is not None:
        green = pipe._green(red)
        if not green:
            record.remaining = [
                f for f in record.remaining if f.get("file") != GREEN_FILE
            ]
        else:
            green = settle(record, green, fix.text, red[1])
    return last, green
