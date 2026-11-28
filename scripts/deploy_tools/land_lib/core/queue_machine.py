"""Who takes a free landing turn, and which drafts its holder stacks, as pure functions.

`turn._wait` asks `may_take` before each try of the lock, and `batch._members` stacks what
`stackable` yields. Both serve the landings first come, first served, by the ticket each holds
(`line.py`): a landing tries the lock only when no fresh place holds an older ticket, and a
holder stacks the oldest drafts first. The `flock` stays the only mutual exclusion. Two
landings that both read themselves as oldest both try it, and it admits one.
"""

from collections.abc import Iterator

from deploy_tools.land_lib.core.queue_state import Place, Queue


def _order(place: Place) -> tuple[int, str]:
    return place.ticket, place.pr


def may_take(queue: Queue, me: Place) -> bool:
    """Whether the landing at `me` may try the lock of the turn now.

    It may when no place in `queue` holds an older ticket than its own. Equal tickets go in
    PR order. A place with no ticket is older than every ticket, so a landing on older code,
    which holds none, is never kept waiting by one that does.
    """
    return all(_order(me) <= _order(place) for place in queue.places)


def stackable(queue: Queue) -> Iterator[Place]:
    """The drafts a holder may stack behind its own PR, in the order it stacks them.

    Each place with a head, oldest ticket first, and only those `queue.lands_alone` refuses.
    It asks `lands_alone` of one place as the caller takes it, so a caller that stops at a
    batch's cap asks no further.
    """
    for place in sorted((p for p in queue.places if p.head), key=_order):
        if not queue.lands_alone(place):
            yield place
