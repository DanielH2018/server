"""The merge wait as a pure transition function: `step(LandingState, Observation)`.

`start` returns the first state and the first actions. Each later call takes the answer to
the last action `step` returned and returns the next state and the next actions, the values
`landing_actions` defines. Every action but the last of a list needs no answer: `Say`,
`Enter` and `Progress` only report. The last one is a call whose answer goes to the next
`step`, or `Die` or `Merged`, which end the wait.

The driver carries each action out through `ln.tools` and the module that owns it, and builds
the answer from what came back. `step` reads no clock, sleeps for nothing and calls nothing.

`merge.await_merge`'s loop makes the same calls in the same order. The characterization
traces in `scripts/deploy_tools/tests/land_merge/` hold `step` to that loop.

Typical usage example:

    state, actions = start(LandingState(setup, pinned_head=pinned))
    while not isinstance(actions[-1], landing_actions.ENDS):
        state, actions = step(state, perform(actions))
"""

from dataclasses import replace

from deploy_tools.land_lib.core.landing_actions import (
    Action,
    Answer,
    AskTurn,
    CheckPolicy,
    CiRateLimited,
    CiRead,
    CommitsRead,
    Die,
    Done,
    End,
    Enter,
    FindFailing,
    FindStuck,
    GatePins,
    Merge,
    MergeAnswered,
    Merged,
    Names,
    Observation,
    PinsGated,
    PolicyChecked,
    Progress,
    ReadCi,
    ReadCommits,
    ReadTested,
    ReadTurnLost,
    ReadView,
    RecheckPins,
    RefuseBeforeMerge,
    RefuseStaleOverlap,
    RejoinInTurn,
    RejoinLine,
    RunStarted,
    Say,
    Sleep,
    Slept,
    StallWatched,
    StartRun,
    StepOut,
    Tested,
    TurnAnswered,
    Viewed,
    WaitOutRateLimit,
    WatchStall,
)
from deploy_tools.land_lib.core.landing_state import (
    Ci,
    LandingState,
    Point,
    Poll,
    State,
)


type Step = tuple[LandingState, list[Action]]


def _wait(s: LandingState, action: Action, at: Point, *said: Action) -> Step:
    return replace(s, awaiting=at), [*said, action]


def _bounded(s: LandingState, waited: int) -> int:
    """The end of the wait after a wait in line or an update, inside two merge budgets."""
    return min(waited + s.setup.merge_timeout, s.wait_cap)


def _rejoined(s: LandingState, head: str, *, in_turn: bool) -> LandingState:
    return replace(s, in_turn=in_turn, updated_from=head, rejoined=True, drafted=True)


def start(s: LandingState) -> Step:
    """The first actions for `s`, the state the driver read before the first call.

    A landing that merges the PR itself gives a head with no CI run its run first.
    """
    if s.setup.direct:
        return _wait(s, StartRun(rejoined=False, drafted=False), Point.START)
    return _begin(s)


def _begin(s: LandingState) -> Step:
    """The wait's budget from where the start left `waited`, then the first poll."""
    ends = s.waited + s.setup.merge_timeout
    if s.batched:
        ends = min(ends, s.wait_cap)
    return _poll(replace(s, wait_ends_at=ends))


def _poll(s: LandingState, *said: Action) -> Step:
    return _wait(s, ReadView(), Point.VIEW, *said)


def step(s: LandingState, answer: Observation) -> Step:
    """The next state and actions, given the answer to the last action `step` returned.

    Raises:
        TypeError: `answer` is not the kind the awaited call returns.
    """
    handler = _HANDLERS[s.awaiting]
    return handler(s, answer)


def _on_start(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, RunStarted):
        raise TypeError(a)
    s = replace(
        s,
        updated_from=a.updated_from,
        in_turn=a.in_turn,
        queued=a.in_line,
        waited=a.in_line,
        asked=s.asked or a.asked,
        tested_head=a.tested_head,
        batched=a.batched,
    )
    return _begin(s)


def _on_view(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Viewed):
        raise TypeError(a)
    if a.pr_state == "MERGED":
        return replace(s, awaiting=Point.DONE), [Enter(State.MERGED), Merged(s.waited)]
    if a.pr_state == "CLOSED":
        return replace(s, awaiting=Point.DONE), [Die(End.CLOSED)]
    conflicts = s.conflicts + 1 if a.conflicting else 0
    s = replace(s, conflicts=conflicts)
    if conflicts >= 2:
        return replace(s, awaiting=Point.DONE), [Die(End.CONFLICT)]
    # GitHub applies a branch update asynchronously: until the head moves, the stale head's
    # green CI is what the update discards.
    head = "" if s.updated_from and a.head == s.updated_from else a.head
    s = replace(s, poll=Poll(pr_state=a.pr_state, head=head, conflicting=a.conflicting))
    if s.pinned_head and head and head != s.pinned_head:
        if s.updated_from != s.pinned_head:
            die = Die(End.HEAD_MOVED, head=head, detail=s.pinned_head)
            return replace(s, awaiting=Point.DONE), [die]
        say = Say(f"checking the landing policy again on the updated head {head[:8]}")
        return _wait(s, CheckPolicy(), Point.POLICY, say)
    if s.rejoined and head:
        s = replace(s, rejoined=False)
        return _wait(s, StartRun(rejoined=True, drafted=s.drafted), Point.RESTART)
    if head:
        return _wait(s, ReadCi(head), Point.CI)
    return _timeout_or_sleep(s)


def _on_policy(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, PolicyChecked):
        raise TypeError(a)
    return _poll(replace(s, pinned_head=a.pinned_head))


def _on_restart(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, RunStarted):
        raise TypeError(a)
    waited = s.waited + a.in_line
    s = replace(
        s,
        updated_from=a.updated_from or s.updated_from,
        in_turn=a.in_turn,
        queued=s.queued + a.in_line,
        waited=waited,
        wait_ends_at=_bounded(s, waited),
        asked=s.asked or a.asked,
        tested_head=a.tested_head,
    )
    return _poll(s)


def _ci_state(s: LandingState, head: str) -> State:
    """The state of a landing whose head's CI is pending: its own run, or a run in the turn."""
    if not s.in_turn:
        return State.OWN_RUN
    return State.PROVING if head == s.tested_head else State.TESTING


def _on_ci(s: LandingState, a: Observation) -> Step:
    head = s.poll.head
    if isinstance(a, CiRateLimited):
        s = replace(s, poll=replace(s.poll, pause=a.wait_s, quota=a.quota))
        text = f"{head[:8]}: {a.message}; the merge wait backs off until it does"
        if text == s.said_rate_limit:
            return _timeout_or_sleep(s)
        state, actions = _timeout_or_sleep(replace(s, said_rate_limit=text))
        return state, [Say(text), *actions]
    if not isinstance(a, CiRead):
        raise TypeError(a)
    pending = a.ci is Ci.PENDING
    s = replace(
        s,
        poll=replace(s.poll, ci=a.ci, line=a.line),
        ci_pending=a.line if pending else "",
    )
    said: list[Action] = []
    # A head that asked and has no turn stays `no-turn`.
    if pending and (s.in_turn or not s.asked):
        said.append(Enter(_ci_state(s, head)))
        if a.checks:
            said.append(Progress(*a.checks))
    watch = WatchStall(s.watch, head, a.stall if pending else None, s.waited, a.line)
    return _wait(s, watch, Point.STALL, *said)


def _on_stall(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, StallWatched):
        raise TypeError(a)
    s = replace(s, watch=a.watch)
    p = s.poll
    if p.ci is Ci.RED:
        return replace(s, awaiting=Point.DONE), [Die(End.CI_RED, detail=p.line)]
    # Only this landing's direct merge waits on await_ci's green, so only it can never fire on
    # a disarmed gate.
    if p.ci is Ci.DISARMED and s.setup.direct:
        return replace(s, awaiting=Point.DONE), [Die(End.DISARMED, head=p.head)]
    if p.ci is Ci.GREEN and s.setup.direct and not s.in_turn:
        if not s.asked:
            return _wait(s, RejoinLine(p.head, p.conflicting), Point.REJOIN_LINE)
        return _ask(s)
    if p.ci is Ci.GREEN and s.setup.direct:
        return _merge(s)
    return _stuck(s)


def _ask(s: LandingState) -> Step:
    s = replace(s, asked=True)
    return _wait(s, AskTurn(s.poll.head, s.drafted), Point.ASK)


def _on_rejoin_line(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Answer):
        raise TypeError(a)
    if not a.yes:
        return _ask(s)
    s = _rejoined(s, s.poll.head, in_turn=s.in_turn)
    s = replace(s, wait_ends_at=_bounded(s, s.waited))
    return _poll(s, Enter(State.REJOINING))


def _on_ask(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, TurnAnswered):
        raise TypeError(a)
    s = replace(s, in_turn=a.in_turn, tested_head=a.tested_head)
    if not a.in_line:
        return _merge(s)
    # The PR may have moved while this landing stood in line, so it is read again.
    waited = s.waited + a.in_line
    s = replace(
        s, queued=s.queued + a.in_line, waited=waited, wait_ends_at=_bounded(s, waited)
    )
    p = s.poll
    return _wait(s, RejoinInTurn(p.head, p.conflicting), Point.REJOIN_AFTER_WAIT)


def _on_rejoin_after_wait(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Answer):
        raise TypeError(a)
    if not a.yes:
        return _poll(s)
    return _poll(_rejoined(s, s.poll.head, in_turn=False), Enter(State.REJOINING))


def _merge(s: LandingState) -> Step:
    head = s.poll.head
    return _wait(s, RefuseBeforeMerge(head), Point.BEFORE_MERGE, Enter(State.MERGING))


def _on_before_merge(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Done):
        raise TypeError(a)
    head = s.poll.head
    if head != s.gated_head:
        return _wait(s, GatePins(head), Point.GATE)
    return _wait(s, RecheckPins(s.gated_pins), Point.RECHECK)


def _on_gate(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, PinsGated):
        raise TypeError(a)
    s = replace(s, gated_pins=a.pins, gated_head=s.poll.head)
    return _wait(s, ReadCommits(), Point.COMMITS)


def _on_recheck(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Done):
        raise TypeError(a)
    return _wait(s, ReadCommits(), Point.COMMITS)


def _on_commits(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, CommitsRead):
        raise TypeError(a)
    s = replace(s, commits=a.commits)
    return _wait(s, ReadTested(s.poll.head), Point.TESTED)


def _on_tested(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Tested):
        raise TypeError(a)
    p = s.poll
    if a.tested:
        return _wait(s, ReadTurnLost(), Point.LOST)
    if a.interrupted:
        return _wait(s, RejoinInTurn(p.head, p.conflicting), Point.REJOIN_INTERRUPTED)
    return _wait(s, RefuseStaleOverlap(p.head), Point.OVERLAP)


def _on_rejoin_interrupted(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Answer):
        raise TypeError(a)
    if a.yes:
        s = _rejoined(s, s.poll.head, in_turn=False)
        return _poll(s, Enter(State.REJOINING))
    return _wait(s, RefuseStaleOverlap(s.poll.head), Point.OVERLAP)


def _on_overlap(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Done):
        raise TypeError(a)
    return _wait(s, ReadTurnLost(), Point.LOST)


def _on_lost(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Answer):
        raise TypeError(a)
    if a.yes:
        out = StepOut("because the ssh session that held it ended")
        return _wait(s, out, Point.STEP_OUT)
    return _wait(s, Merge(s.poll.head, s.commits), Point.MERGE)


def _on_step_out(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Done):
        raise TypeError(a)
    return _poll(s)


def _on_merge(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, MergeAnswered):
        raise TypeError(a)
    if not a.refusal:
        return _poll(s)
    s = replace(s, poll=replace(s.poll, refusal=a.refusal))
    return _wait(s, FindFailing(s.poll.head, a.refusal), Point.FAILING)


def _on_failing(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Names):
        raise TypeError(a)
    head = s.poll.head
    if a.names:
        die = Die(End.FAILING_CHECK, head=head, names=a.names)
        return replace(s, awaiting=Point.DONE), [die]
    text = f"merge refused at {head[:8]}: {s.poll.refusal}; waiting"
    if text == s.said_refusal:
        return _stuck(s)
    return _stuck(replace(s, said_refusal=text), Say(text))


def _stuck(s: LandingState, *said: Action) -> Step:
    """The read for a check GitHub never finishes, on a head the wait has sat on long enough."""
    head = s.poll.head
    if head != s.stuck_head:
        s = replace(s, stuck_head=head, stuck_read_at=s.waited + s.setup.stuck_after_s)
    elif s.poll.ci is Ci.GREEN and s.waited >= s.stuck_read_at:
        s = replace(s, stuck_read_at=s.waited + s.setup.stuck_after_s)
        return _wait(s, FindStuck(head), Point.STUCK, *said)
    state, actions = _timeout_or_sleep(s)
    return state, [*said, *actions]


def _on_stuck(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Names):
        raise TypeError(a)
    if a.names:
        die = Die(End.STUCK_CHECK, head=s.poll.head, names=a.names)
        return replace(s, awaiting=Point.DONE), [die]
    return _timeout_or_sleep(s)


def _timeout_or_sleep(s: LandingState) -> Step:
    """End the wait once its budget is spent; otherwise sleep until the next poll."""
    p = s.poll
    if s.waited >= s.wait_ends_at:
        # A run skipped as a draft names its own remedy in await_ci's line, because a re-run
        # skips again.
        if p.pause:
            end, detail = End.TIMEOUT_RATE_LIMITED, str(p.pause)
        elif s.watch.head:
            end, detail = End.TIMEOUT_STALLED, s.ci_pending
        elif s.ci_pending:
            end, detail = End.TIMEOUT_PENDING, s.ci_pending
        else:
            end, detail = End.TIMEOUT, ""
        die = Die(end, detail=detail, waited=s.waited, pr_state=p.pr_state)
        return replace(s, awaiting=Point.DONE), [die]
    poll = s.setup.merge_poll
    if p.pause:
        seconds = max(poll, min(p.pause, s.wait_ends_at - s.waited))
        return _wait(s, WaitOutRateLimit(p.quota, seconds), Point.SLEEP)
    return _wait(s, Sleep(poll), Point.SLEEP)


def _on_sleep(s: LandingState, a: Observation) -> Step:
    if not isinstance(a, Slept):
        raise TypeError(a)
    return _poll(replace(s, waited=s.waited + a.seconds))


def _on_done(_s: LandingState, a: Observation) -> Step:
    raise TypeError(f"the merge wait has ended; {a} answers nothing")


_HANDLERS = {
    Point.START: _on_start,
    Point.VIEW: _on_view,
    Point.POLICY: _on_policy,
    Point.RESTART: _on_restart,
    Point.CI: _on_ci,
    Point.STALL: _on_stall,
    Point.REJOIN_LINE: _on_rejoin_line,
    Point.ASK: _on_ask,
    Point.REJOIN_AFTER_WAIT: _on_rejoin_after_wait,
    Point.BEFORE_MERGE: _on_before_merge,
    Point.GATE: _on_gate,
    Point.RECHECK: _on_recheck,
    Point.COMMITS: _on_commits,
    Point.TESTED: _on_tested,
    Point.REJOIN_INTERRUPTED: _on_rejoin_interrupted,
    Point.OVERLAP: _on_overlap,
    Point.LOST: _on_lost,
    Point.STEP_OUT: _on_step_out,
    Point.MERGE: _on_merge,
    Point.FAILING: _on_failing,
    Point.STUCK: _on_stuck,
    Point.SLEEP: _on_sleep,
    Point.DONE: _on_done,
}
