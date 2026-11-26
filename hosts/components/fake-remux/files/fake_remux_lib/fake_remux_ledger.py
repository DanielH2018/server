"""The fake-remux ledger's states, and the moves between them.

The ledger maps `str(episodeId)` to one fake's record. The scan adds a record as `detected`, the
reconciler moves it, and every move goes through `move`, which refuses one `TRANSITIONS` does not
list.

Stdlib-only, and pure.
"""

from enum import StrEnum


class State(StrEnum):
    """Where one fake's replacement stands. The ledger file holds the value as a plain string."""

    DETECTED = "detected"
    GRABBED = "grabbed"
    VERIFYING = "verifying"
    IMPORTING = "importing"
    REPLACED = "replaced"
    HELD = "held"


# A download exists for the episode, so it counts against the concurrency cap.
IN_FLIGHT = frozenset({State.GRABBED, State.VERIFYING, State.IMPORTING})

# `held` and `replaced` end a record. A human clears a held one from the file by hand.
TRANSITIONS = {
    State.DETECTED: frozenset({State.GRABBED, State.HELD}),
    State.GRABBED: frozenset(
        {
            State.DETECTED,
            State.VERIFYING,
            State.IMPORTING,
            State.REPLACED,
            State.HELD,
        }
    ),
    State.VERIFYING: frozenset({State.DETECTED, State.IMPORTING, State.HELD}),
    State.IMPORTING: frozenset({State.REPLACED, State.HELD}),
    State.REPLACED: frozenset(),
    State.HELD: frozenset(),
}


def parse(raw):
    """The ledger `raw` holds, each record's state a `State`.

    Raises:
        ValueError: a record has no state, or one that is not a `State`. No branch would act on
            it, so it would count as in flight for ever.
    """
    out = {}
    for ep, rec in raw.items():
        try:
            state = State(rec.get("state"))
        except ValueError as e:
            raise ValueError("ledger entry %s: %s" % (ep, e)) from e
        out[ep] = {**rec, "state": state}
    return out


def move(ledger, ep, to, **changes):
    """A copy of `ledger` with the record for `ep` in state `to`, and `changes` applied to it.

    Raises:
        ValueError: `TRANSITIONS` does not list the move.
    """
    key = str(ep)
    rec = ledger[key]
    if to not in TRANSITIONS[State(rec["state"])]:
        raise ValueError("ledger entry %s: no move %s -> %s" % (key, rec["state"], to))
    return {**ledger, key: {**rec, **changes, "state": to}}
