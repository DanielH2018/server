"""A pure model of who takes the landing turn next: the lock, the polls and the line.

Each `Queue` method is one event, with no I/O and no clock. `Policy.RACE` is what the code
does: `turn._wait` tries the lock before every sleep, `turn.held`'s `step_out` asks for the turn
back with no draft, and `line._op` lists places by file name. `Policy.TICKETS` is the proposal
in `docs/landing-architecture.md`: a landing takes a free turn only when no fresh ticket is
older than its own, a holder stacks the oldest drafts, and a landing that steps out of the turn
keeps its place.

The model leaves out a batch that is not green, a turn handed to a fresh checkout, and the
`merge_timeout` after which a landing goes on without the turn.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from deploy_tools.land_lib import batch, line
from deploy_tools.land_lib.options import MERGE_POLL_S

BATCH_FULL = "a batch held every PR it may"
OVERTAKEN = "a later arrival took the turn first"
DEFERRED_TO_DEAD = "a landing left the turn free for a dead landing's ticket"
STEPPED_OUT = "a holder stepped out of the turn"


class Policy(StrEnum):
    """Which landing a free turn goes to."""

    RACE = "race"
    TICKETS = "tickets"


@dataclass
class Waiter:
    """One landing that waits for the turn, or holds it.

    Attributes:
        arrived: when it first asked for the turn, which is its ticket.
        draft: whether its PR is a draft with no CI run, so it forms a batch with the turn.
        in_line: whether it stands in the line, where a holder can stack it.
        eligible_since: when it could last use the turn; later than `now` while it waits
            outside the turn for a serial landing or a rate limit.
        died_at: when its process ended without leaving the line, or None while it lives.
        overtaken: the turns granted to a later arrival while it lived and could use one.
    """

    pr: int
    arrived: int
    draft: bool
    serial: bool
    in_line: bool
    next_poll: int
    eligible_since: int
    died_at: int | None = None
    overtaken: int = 0

    @property
    def ticket(self) -> tuple[int, int]:
        return self.arrived, self.pr


class Queue:
    """The turn, the landings that wait for it, and the batches its holders formed."""

    def __init__(
        self, policy: Policy, record: Callable[[str], None] = lambda _event: None
    ) -> None:
        self.policy = policy
        self.record = record
        self.now = 0
        self.holder: Waiter | None = None
        self.waiting: dict[int, Waiter] = {}
        self.free_since = 0
        self.last_death = 0
        self.batches: list[list[int]] = []

    def _fresh(self, w: Waiter) -> bool:
        return w.died_at is None or self.now - w.died_at < line.FRESH_S

    def _can_use_turn(self, w: Waiter) -> bool:
        return w.died_at is None and w.eligible_since <= self.now

    def _may_take(self, w: Waiter) -> bool:
        if self.policy is Policy.RACE:
            return True
        older = [
            o
            for o in self.waiting.values()
            if o.ticket < w.ticket and o.eligible_since <= self.now and self._fresh(o)
        ]
        if older and all(o.died_at is not None for o in older):
            self.record(DEFERRED_TO_DEAD)
        return not older

    def stackable(self) -> list[Waiter]:
        """The landings a holder may stack, in the order it reads them from the line."""
        found = [
            w
            for w in self.waiting.values()
            if w.in_line and not w.serial and self._can_use_turn(w)
        ]
        if self.policy is Policy.RACE:
            return sorted(found, key=lambda w: str(w.pr))
        return sorted(found, key=lambda w: w.ticket)

    def _grant(self, w: Waiter) -> None:
        del self.waiting[w.pr]
        self.holder = w
        for other in self.waiting.values():
            if self._can_use_turn(other) and other.ticket < w.ticket:
                other.overtaken += 1
                self.record(OVERTAKEN)
        members: list[Waiter] = []
        if w.draft and not w.serial:
            members = self.stackable()[: batch.MAX_STACKED - 1]
        for member in members:
            del self.waiting[member.pr]
        if len(members) == batch.MAX_STACKED - 1:
            self.record(BATCH_FULL)
        self.batches.append([w.pr, *(m.pr for m in members)])

    def _poll(self, w: Waiter) -> None:
        if self.holder is None and self._may_take(w):
            self._grant(w)
        else:
            w.next_poll = self.now + MERGE_POLL_S

    def arrive(self, pr: int, draft: bool, serial: bool = False) -> None:
        """A landing asks for the turn, and takes it at once when the policy lets it."""
        w = Waiter(pr, self.now, draft, serial, draft, self.now, self.now)
        self.waiting[pr] = w
        self._poll(w)

    def release(self) -> None:
        """The holder merges, or ends, and gives the turn up."""
        assert self.holder is not None
        self.holder, self.free_since = None, self.now

    def step_out(self, wait_s: int) -> None:
        """The holder gives the turn back for a wait of `wait_s`, then asks for it again."""
        w = self.holder
        assert w is not None
        self.holder, self.free_since = None, self.now
        w.eligible_since = w.next_poll = self.now + wait_s
        if self.policy is Policy.RACE:
            w.in_line = False
        self.waiting[w.pr] = w
        self.record(STEPPED_OUT)

    def die(self, pr: int) -> None:
        """A waiting landing's process ends without leaving the line."""
        self.waiting[pr].died_at = self.last_death = self.now

    def tick(self, seconds: int) -> None:
        """Let `seconds` pass, running each poll that falls due in time order."""
        end = self.now + seconds
        while True:
            due = [
                w
                for w in self.waiting.values()
                if w.died_at is None and w.next_poll <= end
            ]
            if not due:
                break
            w = min(due, key=lambda w: (w.next_poll, w.pr))
            self.now = max(self.now, w.next_poll)
            self._poll(w)
        self.now = end
        for pr, w in list(self.waiting.items()):
            if not self._fresh(w):
                del self.waiting[pr]

    def stuck(self) -> list[int]:
        """The landings that could use a turn that has stayed free past the policy's bound.

        Under the race a landing takes a free turn on its next poll. With tickets it may
        first wait out a dead landing's ticket, which stays fresh for `line.FRESH_S`.
        """
        if self.holder is not None:
            return []
        bound = MERGE_POLL_S
        since = self.free_since
        if self.policy is Policy.TICKETS:
            bound += line.FRESH_S
            since = max(since, self.last_death)
        return [
            w.pr
            for w in self.waiting.values()
            if self._can_use_turn(w) and self.now - max(since, w.eligible_since) > bound
        ]
