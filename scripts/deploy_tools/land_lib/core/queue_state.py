"""The queue for the landing turn, as the turn's ordering decisions read it.

`queue_machine` decides over these records and does no I/O. The landing builds them from what
it reads: `turn._wait` from its own place and the fresh tickets its stand reads,
`batch._members` from the drafts' places the line lists (`line.py`).
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    """One landing that waits for the turn.

    Attributes:
        pr: Its PR's number, as the line names the PR's file.
        head: The head of its draft that has had no CI run, which a holder can stack, or ""
            for a landing with nothing to stack.
        ticket: When its landing first asked for the turn, in nanoseconds on the turn host's
            clock. 0 for a place a landing on older code wrote, which holds no ticket.
    """

    pr: str
    head: str = ""
    ticket: int = 0


def _stacks(_place: Place) -> bool:
    return False


@dataclass(frozen=True)
class Queue:
    """The landings that wait for the turn.

    Attributes:
        places: Each waiting landing's place, in any order.
        lands_alone: Whether a place's PR must land alone, as a serial PR does
            (`live_limit.SERIAL`). `queue_machine.stackable` asks it of one place at a time,
            in stacking order, so a caller that reads a PR's files to answer stops reading
            where it stops stacking.
    """

    places: tuple[Place, ...]
    lands_alone: Callable[[Place], bool] = _stacks
