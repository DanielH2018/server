"""One landing's state through the merge wait, as `landing_machine.step` reads and returns it.

`State` names the states of *The landing state machine* in `docs/landing-architecture.md`, as
the state record and the transition line name them (`transitions.py`). `LandingState` is every
fact the merge wait carries from one call to the next, in one frozen record. `step` returns a
new one for each answer it is given, and nothing else holds a copy.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    """One state of a landing, as the state record and the transition line name it."""

    ARMED = "armed"
    HANDED_OFF = "handed-off"
    OWN_RUN = "own-run"
    QUEUED = "queued"
    HOLDING = "holding"
    YIELDED = "yielded"
    TESTING = "testing"
    SPLIT = "split"
    PROVING = "proving"
    MERGING = "merging"
    REJOINING = "rejoining"
    NO_TURN = "no-turn"
    MERGED = "merged"
    RESOLVING = "resolving"
    MASTER_CI = "master-ci"
    PROMOTING = "promoting"
    HOST_WAIT = "host-wait"
    GRADING = "grading"
    VERDICT = "verdict"


class Stall(StrEnum):
    """Why a head's CI will not move without the landing."""

    DRAFT_SKIPPED = "draft-skipped"
    NO_RUN_LEFT = "no-run-left"


@dataclass(frozen=True)
class Watch:
    """The stall the wait is sitting on, carried from one poll to the next.

    Attributes:
        head: the stalled head, or "" when the last poll read no stall.
        stall: that head's stall, or None.
        since: the `waited` its grace started at.
        tried: each (head, stall) the landing already started a run for.
    """

    head: str = ""
    stall: Stall | None = None
    since: int = 0
    tried: frozenset[tuple[str, Stall]] = frozenset()

    def retried(self) -> bool:
        """Whether the landing already started a run for the stall it sits on."""
        return self.stall is not None and (self.head, self.stall) in self.tried


class Ci(StrEnum):
    """What one await_ci read answered for a head."""

    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    DISARMED = "disarmed"
    # An exit code await_ci documents none of; the wait polls on.
    OTHER = "other"


class Point(StrEnum):
    """The call whose answer `step` takes next."""

    START = "start"
    VIEW = "view"
    POLICY = "policy"
    RESTART = "restart"
    CI = "ci"
    STALL = "stall"
    REJOIN_LINE = "rejoin-line"
    ASK = "ask"
    REJOIN_AFTER_WAIT = "rejoin-after-wait"
    BEFORE_MERGE = "before-merge"
    GATE = "gate"
    RECHECK = "recheck"
    COMMITS = "commits"
    TESTED = "tested"
    REJOIN_INTERRUPTED = "rejoin-interrupted"
    OVERLAP = "overlap"
    LOST = "lost"
    STEP_OUT = "step-out"
    MERGE = "merge"
    FAILING = "failing"
    STUCK = "stuck"
    SLEEP = "sleep"
    DONE = "done"


@dataclass(frozen=True)
class Setup:
    """What the merge wait reads once, before its first call.

    Attributes:
        pr: The PR's number.
        merge_timeout: The seconds of one merge budget.
        merge_poll: The seconds between two polls.
        direct: Whether this landing merges the PR itself, once its head reads green.
        stuck_after_s: How long a green head waits before its checks are read for one
            GitHub never finishes, and between two such reads.
    """

    pr: str
    merge_timeout: int
    merge_poll: int
    direct: bool
    stuck_after_s: int


@dataclass(frozen=True)
class Poll:
    """What the poll in progress has read.

    Attributes:
        pr_state: The PR's `state`, as GitHub names it.
        head: The head the poll acts on, or "" for none: a branch update GitHub has yet to
            apply reads as none.
        conflicting: Whether GitHub read the PR as conflicting on this poll.
        ci: await_ci's answer for `head`; PENDING until it is read.
        line: await_ci's one-line answer.
        pause: The seconds until a rate-limited CI read's quota frees; 0 when none was.
        quota: The spent quota's name for that wait, or "" for a secondary limit.
        refusal: GitHub's refusal of this poll's merge, or "".
    """

    pr_state: str = ""
    head: str = ""
    conflicting: bool = False
    ci: Ci = Ci.PENDING
    line: str = ""
    pause: int = 0
    quota: str = ""
    refusal: str = ""


@dataclass(frozen=True)
class LandingState:
    """Every fact the merge wait carries from one call to the next.

    Attributes:
        setup: What the wait read before it started.
        awaiting: The call whose answer `step` takes next.
        poll: What the poll in progress has read.
        waited: The seconds the wait has counted, the wait in line included.
        wait_ends_at: The `waited` at which the wait ends.
        batched: Whether a landing batch's run counted toward `waited`, which caps
            `wait_ends_at` at two merge budgets from the start.
        in_turn: Whether this landing holds the landing turn. An answer whose call ended
            without the turn clears it (`landing_actions._Answered`).
        asked: Whether this landing has asked for the turn.
        queued: The seconds it stood in line for the turn.
        tested_head: The head a landing batch tested, whose pending run is Proving.
        conflicts: How many polls in a row read the PR as conflicting.
        ci_pending: await_ci's last answer while it was pending, for the timeout's message.
        updated_from: The head a branch update or a rejoin moved the PR off, whose green the
            update discards; "" for none.
        rejoined: Whether the PR rejoined the line as a draft at a head GitHub has yet to move.
        drafted: Whether this landing made the PR a draft, so a refusal in the turn marks it
            ready again.
        pinned_head: The head the landing policy checked, or "" without the policy.
        gated_head: The head the pin gate last passed.
        gated_pins: The forward-only pins that head bumps.
        watch: The stall the wait sits on (`stall.step`).
        stuck_head: The head whose checks the stuck-check read covers.
        stuck_read_at: The `waited` of that head's next stuck-check read.
        commits: The PR's commits, read for the merge in progress; opaque to `step`.
        said_rate_limit: The last rate-limit line the wait printed.
        said_refusal: The last merge refusal the wait printed.
    """

    setup: Setup
    awaiting: Point = Point.START
    poll: Poll = Poll()
    waited: int = 0
    wait_ends_at: int = 0
    batched: bool = False
    in_turn: bool = False
    asked: bool = False
    queued: int = 0
    tested_head: str = ""
    conflicts: int = 0
    ci_pending: str = ""
    updated_from: str = ""
    rejoined: bool = False
    drafted: bool = False
    pinned_head: str = ""
    gated_head: str = ""
    gated_pins: tuple[str, ...] = ()
    watch: Watch = Watch()
    stuck_head: str = ""
    stuck_read_at: int = 0
    commits: tuple[object, ...] = field(default=())
    said_rate_limit: str = ""
    said_refusal: str = ""

    @property
    def wait_cap(self) -> int:
        """The `waited` no update moves the end of the wait past: two merge budgets."""
        return 2 * self.setup.merge_timeout
