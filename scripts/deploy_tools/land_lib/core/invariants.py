"""The invariants of `docs/landing-architecture.md` that the turn's ordering decisions bear on.

Each is a predicate over plain values, and the Hypothesis state machine in
`scripts/deploy_tools/tests/land_merge/test_land_queue_machine.py` checks every one after every
step it drives through `queue_machine`.
"""

from collections.abc import Collection, Iterable, Sequence


def serial_lands_alone(batch: Sequence[str], serial: Collection[str]) -> bool:
    """A batch of more than one PR holds no serial PR, the holder's own included.

    The batch half of the *Serial alone* invariant.
    """
    return len(batch) == 1 or not set(batch) & set(serial)


def free_turn_is_taken(
    free_since: int, now: int, ready_since: Iterable[int], bound: int
) -> bool:
    """No landing that can use the turn finds it free for longer than `bound`.

    Args:
        free_since: When the turn was last given up; the turn is free now.
        now: The time of the check.
        ready_since: For each live landing that waits for the turn, when it could last use
            it: its arrival, or the end of a wait outside the turn.
        bound: The longest a free turn may stay so while a landing could take it.
    """
    return all(now - max(free_since, ready) <= bound for ready in ready_since)
