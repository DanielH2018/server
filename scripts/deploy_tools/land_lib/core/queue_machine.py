"""Who takes a free landing turn, and which drafts its holder stacks, as pure functions.

`turn._wait` asks `may_take` before each try of the lock, and `batch._members` stacks what
`stackable` yields. The rules are the race the landing has always run: a landing tries a free
turn on every poll, so the first poll after a release takes it, and a holder stacks the drafts
in the order the line names their files. Slice 3 of `docs/landing-architecture.md` replaces
the race with tickets inside these two functions.
"""

from collections.abc import Iterator

from deploy_tools.land_lib.core.queue_state import Place, Queue


def may_take(_queue: Queue, _me: Place) -> bool:
    """Whether the landing at `_me` may try the lock of the turn now.

    Under the race every landing may: the `flock` admits one of those that try, so the turn
    goes to whichever polls first. Tickets read `_queue` for an older ticket.
    """
    return True


def stackable(queue: Queue) -> Iterator[Place]:
    """The drafts a holder may stack behind its own PR, in the order it stacks them.

    Each place with a head, in the order the line's file names sort in, so #10458 comes
    before #9998, and only those `queue.lands_alone` refuses. It asks `lands_alone` of one
    place as the caller takes it, so a caller that stops at a batch's cap asks no further.
    """
    for place in sorted((p for p in queue.places if p.head), key=lambda p: p.pr):
        if not queue.lands_alone(place):
            yield place
