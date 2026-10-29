"""Which findings the delta review leaves standing at the fix head, and which it only repeats.

The delta reviewer is asked to report each earlier finding the fix does not resolve. A model
that answers from the first review's text, or from the tree before the fix, reports a finding
the fix commit already fixed, and the pipeline used to take every reported finding as left
(#6547: PR #6512 was held on two findings whose tests fail before the fix commit and pass at it).

A reported finding now counts as left only when its `evidence`, a line it quotes, is in the
file at the fix head within `WINDOW` lines of the line it cites. Any other finding is carried
as unverified: the PR comment and the local record name it, and it does not hold the PR.
"""

from collections.abc import Callable

# The red/green gate's finding names no source file, so there is no line to quote at the head.
# `green_contest` judges it from the red tests themselves.
from fanout_lib.review.red_gate import GREEN_FILE

# A fix that adds or removes a few lines moves the cited line (#6512: 624 moved to 627 and
# 643 to 647), so the quote is looked for around the line, not on it.
WINDOW = 10

# Reads a file as the fix head holds it, or None when it does not exist there.
ReadAtHead = Callable[[str], str | None]


def _squash(text: str) -> str:
    return " ".join(text.split())


def holds_at_head(finding: dict, read: ReadAtHead) -> bool:
    """Whether the finding quotes a line that the fix head's file has near the cited line."""
    if finding.get("file") == GREEN_FILE:
        return True
    quote = _squash(str(finding.get("evidence") or ""))
    text = read(str(finding.get("file") or ""))
    if not quote or text is None:
        return False
    line = finding.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return quote in _squash(text)
    lines = text.splitlines()
    if line > len(lines) + WINDOW:
        return False
    near = lines[max(line - 1 - WINDOW, 0) : line + WINDOW]
    return quote in _squash("\n".join(near))


def split_confirmed(
    left: list[dict], read: ReadAtHead
) -> tuple[list[dict], list[dict]]:
    """`left` as (confirmed at the fix head, carried unverified)."""
    confirmed = [f for f in left if holds_at_head(f, read)]
    return confirmed, [f for f in left if not holds_at_head(f, read)]
