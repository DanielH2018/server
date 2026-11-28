"""What the merge wait's `step` asks for, and the answers it takes, as frozen values.

An action is a call `landing_machine.step` asks the driver to make. Each one's docstring names
the module function that carries it out and the answer it returns. `Say`, `Enter` and
`Progress` only report and take no answer. `Die` and `Merged` end the wait.
"""

from dataclasses import dataclass
from enum import StrEnum

from deploy_tools.land_lib.core.landing_state import Ci, Stall, State, Watch


class End(StrEnum):
    """Why the merge wait ended without a merge; `merge.ending` names the message and verdict."""

    CLOSED = "closed"
    CONFLICT = "conflict"
    HEAD_MOVED = "head-moved"
    CI_RED = "ci-red"
    DISARMED = "disarmed"
    FAILING_CHECK = "failing-check"
    STUCK_CHECK = "stuck-check"
    TIMEOUT_RATE_LIMITED = "timeout-rate-limited"
    TIMEOUT_STALLED = "timeout-stalled"
    TIMEOUT_PENDING = "timeout-pending"
    TIMEOUT = "timeout"


# -- actions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StartRun:
    """`merge._start_run`: give a head with no CI run its one run, in the turn. -> `RunStarted`."""

    rejoined: bool
    drafted: bool


@dataclass(frozen=True)
class ReadView:
    """The PR's `state,mergeable,headRefOid`. -> `Viewed`."""


@dataclass(frozen=True)
class CheckPolicy:
    """`policy.check` on the head a branch update made. -> `PolicyChecked`."""


@dataclass(frozen=True)
class ReadCi:
    """One await_ci read of `head`. -> `CiRead` or `CiRateLimited`."""

    head: str


@dataclass(frozen=True)
class WatchStall:
    """`stall.on_poll` for this poll. -> `StallWatched`."""

    watch: Watch
    head: str
    stall: Stall | None
    waited: int
    line: str


@dataclass(frozen=True)
class RejoinLine:
    """`stale_base.rejoin_line`, before the landing asks for the turn. -> `Answer`."""

    head: str
    conflicting: bool


@dataclass(frozen=True)
class AskTurn:
    """`merge.ask` for a green ready head. -> `TurnAnswered`."""

    head: str
    drafted: bool


@dataclass(frozen=True)
class RejoinInTurn:
    """`stale_base.rejoin_in_turn`, which gives the turn back on a rejoin. -> `Answer`."""

    head: str
    conflicting: bool


@dataclass(frozen=True)
class RefuseBeforeMerge:
    """`slots.refuse_before_merge`. -> `Done`."""

    head: str


@dataclass(frozen=True)
class GatePins:
    """`pin_gate.gate_before_merge`, for a head the gate has not passed. -> `PinsGated`."""

    head: str


@dataclass(frozen=True)
class RecheckPins:
    """`pin_gate.recheck_before_merge`, for the head the gate passed. -> `Done`."""

    pins: tuple[str, ...]


@dataclass(frozen=True)
class ReadCommits:
    """The PR's commits, for the squash body. -> `CommitsRead`."""


@dataclass(frozen=True)
class ReadTested:
    """Whether a batch tested `head` on master's tip, and whether the turn was stepped out of.

    -> `Tested`. The driver clears the step-out it reports.
    """

    head: str


@dataclass(frozen=True)
class RefuseStaleOverlap:
    """`stale_base.refuse_stale_overlap`. -> `Done`."""

    head: str


@dataclass(frozen=True)
class ReadTurnLost:
    """`ln.turn_lost`: whether the ssh session that held the turn ended. -> `Answer`."""


@dataclass(frozen=True)
class StepOut:
    """`ln.without_turn` around nothing: give the turn back and ask for it again. -> `Done`."""

    reason: str


@dataclass(frozen=True)
class Merge:
    """`merge._merge_directly` pinned to `head`. -> `MergeAnswered`."""

    head: str
    commits: tuple[object, ...]


@dataclass(frozen=True)
class FindFailing:
    """`failing_checks.find` for a refused merge. -> `Names`."""

    head: str
    refusal: str


@dataclass(frozen=True)
class FindStuck:
    """`stuck_checks.find`. -> `Names`."""

    head: str


@dataclass(frozen=True)
class Sleep:
    """One merge poll's sleep. -> `Slept`."""

    seconds: int


@dataclass(frozen=True)
class WaitOutRateLimit:
    """`gh_rate_limit.sleep_until_reset` for `quota`, out of the turn. -> `Slept`."""

    quota: str
    seconds: int


@dataclass(frozen=True)
class Say:
    """A line for the landing's log."""

    text: str


@dataclass(frozen=True)
class Enter:
    """`ln.transitions.enter`."""

    state: State


@dataclass(frozen=True)
class Progress:
    """`ln.transitions.progress` with the last CI read's check-runs."""

    done: int
    total: int


@dataclass(frozen=True)
class Die:
    """End the wait without a merge, for `end`; the fields are what its message names."""

    end: End
    head: str = ""
    detail: str = ""
    names: tuple[str, ...] = ()
    waited: int = 0
    pr_state: str = ""


@dataclass(frozen=True)
class Merged:
    """The PR merged after `waited` seconds."""

    waited: int


type Action = (
    StartRun
    | ReadView
    | CheckPolicy
    | ReadCi
    | WatchStall
    | RejoinLine
    | AskTurn
    | RejoinInTurn
    | RefuseBeforeMerge
    | GatePins
    | RecheckPins
    | ReadCommits
    | ReadTested
    | RefuseStaleOverlap
    | ReadTurnLost
    | StepOut
    | Merge
    | FindFailing
    | FindStuck
    | Sleep
    | WaitOutRateLimit
    | Say
    | Enter
    | Progress
    | Die
    | Merged
)


# -- answers --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStarted:
    """What `merge._start_run` returned, and the turn's facts it changed.

    Attributes:
        updated_from: The head it updated the branch from, or "".
        in_turn: Whether the landing holds the turn.
        in_line: The seconds it stood in line.
        asked: Whether it asked for the turn.
        tested_head: The head a batch formed in the turn tested, or "".
        batched: Whether a batch's run counts toward the wait.
    """

    updated_from: str
    in_turn: bool
    in_line: int
    asked: bool
    tested_head: str = ""
    batched: bool = False


@dataclass(frozen=True)
class Viewed:
    """The PR as one poll read it."""

    pr_state: str
    conflicting: bool
    head: str


@dataclass(frozen=True)
class PolicyChecked:
    """The head the landing policy pinned."""

    pinned_head: str


@dataclass(frozen=True)
class CiRead:
    """One await_ci answer; `stall` is what its line names, and `checks` its last count."""

    ci: Ci
    line: str
    stall: Stall | None = None
    checks: tuple[int, int] | None = None


@dataclass(frozen=True)
class CiRateLimited:
    """GitHub refused the CI read for its rate."""

    wait_s: int
    quota: str
    message: str


@dataclass(frozen=True)
class StallWatched:
    """The watch `stall.on_poll` returned."""

    watch: Watch


@dataclass(frozen=True)
class Answer:
    """A yes or no: a rejoin made, a turn lost."""

    yes: bool


@dataclass(frozen=True)
class TurnAnswered:
    """What asking for the turn returned, as `RunStarted` names its fields."""

    in_turn: bool
    in_line: int
    tested_head: str = ""


@dataclass(frozen=True)
class PinsGated:
    """The forward-only pins the gated head bumps."""

    pins: tuple[str, ...]


@dataclass(frozen=True)
class CommitsRead:
    """The PR's commits, passed on to `Merge` unread."""

    commits: tuple[object, ...]


@dataclass(frozen=True)
class Tested:
    """Whether a batch tested the head on master's tip, and whether the turn was stepped out of."""

    tested: bool
    interrupted: bool


@dataclass(frozen=True)
class MergeAnswered:
    """GitHub's refusal of the merge, or "" for a merge it took."""

    refusal: str


@dataclass(frozen=True)
class Names:
    """The checks a read named."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class Done:
    """A call that answers nothing, or ends the landing itself."""


@dataclass(frozen=True)
class Slept:
    """The seconds a sleep took."""

    seconds: int


type Observation = (
    RunStarted
    | Viewed
    | PolicyChecked
    | CiRead
    | CiRateLimited
    | StallWatched
    | Answer
    | TurnAnswered
    | PinsGated
    | CommitsRead
    | Tested
    | MergeAnswered
    | Names
    | Done
    | Slept
)

# The actions that end the wait.
ENDS = (Die, Merged)
