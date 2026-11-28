"""The call trace a merge wait produces for a scenario, and a driver that runs `step`.

`trace` runs a merge wait for a scenario (`_merge_wait_scenarios`) and returns what the wait
did, as text: every boundary call in order, every printed line, and how the wait ended.

`drive` is the merge wait as `core/landing_machine.step` decides it. It carries each action
out through the module that owns it, the same calls `merge.await_merge`'s loop makes, and
builds the answer `step` takes next.
"""

import contextlib
import io
import json
import random
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import cast

from _land_fakes import PRIMARY
from _merge_wait_scenarios import Scenario, TurnBy, arm_member, landing, scenario
from deploy_tools import await_ci
from deploy_tools.land_lib import (
    batch,
    failing_checks,
    merge,
    pin_gate,
    policy,
    slots,
    stale_base,
    stall,
    stuck_checks,
    turn,
)
from deploy_tools.land_lib.core import landing_actions as a
from deploy_tools.land_lib.core import landing_machine
from deploy_tools.land_lib.core.landing_state import Ci, LandingState, Setup
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome, say
from deploy_tools.land_lib.pr_json import CommitMessage
from deploy_tools.land_lib.transitions import CHECKS
from lib import gh_rate_limit
from lib.exit_codes import CI_DISARMED, CI_GREEN, CI_PENDING, CI_RED

CORPUS = Path(__file__).with_name("merge_wait_traces.txt")
_SEED = "# seed "
# The seeds the corpus records, one scenario each.
SEEDS = range(200)
# The PR a batch member's landing lands, beside the holder's.
MEMBER_PR = "1000"
_CI = {
    CI_GREEN: Ci.GREEN,
    CI_RED: Ci.RED,
    CI_PENDING: Ci.PENDING,
    CI_DISARMED: Ci.DISARMED,
}


def _line(name: str, args: tuple, kwargs: dict) -> str | None:
    """One boundary call as trace text, or None for a call the trace leaves out."""
    if name == "record":
        # The state record names the pid, and the transition line beside it holds the same move.
        return None
    words = [name, *(json.dumps(arg, default=str) for arg in args)]
    words += [f"{k}={json.dumps(v, default=str)}" for k, v in kwargs.items() if v]
    return " ".join(words).replace(str(PRIMARY), "<primary>")


type Wait = Callable[[Landing, merge.TakeTurn], None]


class _Printed(io.TextIOBase):
    """A stdout that records each printed line into `calls`, between the calls around it."""

    def __init__(self, calls: list) -> None:
        self._calls = calls
        self._part = ""

    def write(self, text: str) -> int:
        *lines, self._part = (self._part + text).split("\n")
        self._calls.extend(("say", (line,), {}) for line in lines)
        return len(text)


def _offered(ln: Landing, sc: Scenario, wait: Wait) -> None:
    """Run `wait` with the turn offered as `sc.turn_by` says, as that caller offers it."""
    if sc.turn_by is TurnBy.NONE:
        wait(ln, merge._no_turn)
        return
    with turn.held(ln) as take_turn:
        if sc.turn_by is TurnBy.HELD:
            wait(ln, take_turn)
        elif sc.turn_by is TurnBy.BATCH:
            wait(ln, batch.forming(ln, take_turn))
        else:
            take_turn("")
            member = batch._member(ln, MEMBER_PR)
            arm_member(member, sc)
            wait(member, lambda _draft: (True, 0))


def trace(sc: Scenario, wait: Wait) -> list[str]:
    """What `wait` does over the fakes `sc` describes, as lines of text."""
    ln, calls = landing(sc)
    with contextlib.redirect_stdout(_Printed(calls)):
        try:
            _offered(ln, sc, wait)
            end = "merged"
        except Outcome as exc:
            end = f"outcome rc={exc.rc} verdict={exc.verdict} error={exc.error}"
        except Exception as exc:
            end = f"raised {type(exc).__name__}: {exc}"
    return [*(line for c in calls if (line := _line(*c))), end]


def loop(ln: Landing, take_turn: merge.TakeTurn) -> None:
    merge.await_merge(ln, take_turn)


def drive(
    ln: Landing, take_turn: merge.TakeTurn, done: list[a.Action] | None = None
) -> None:
    """The merge wait as `step` decides it, each action carried out over `ln.tools`.

    Each action is appended to `done` before it is carried out.
    """
    o = ln.opts
    setup = Setup(
        pr=str(o.pr),
        merge_timeout=o.merge_timeout,
        merge_poll=o.merge_poll,
        direct=bool(ln.direct_merge_subject),
        stuck_after_s=stuck_checks.AFTER_S,
    )
    s = LandingState(
        setup=setup,
        pinned_head=ln.pinned_head,
        tested_head=ln.tested_head,
        batched=bool(ln.batched_s),
    )
    s, actions = landing_machine.start(s)
    while True:
        answer = None
        for action in actions:
            if done is not None:
                done.append(action)
            answer = perform(ln, take_turn, action)
        if isinstance(actions[-1], a.ENDS):
            return
        assert answer is not None, actions[-1]
        s, actions = landing_machine.step(s, answer)


def perform(
    ln: Landing, take_turn: merge.TakeTurn, action: a.Action
) -> a.Observation | None:
    """Carry out one action; the answer `step` takes, or None for one that only reports."""
    t = ln.tools
    match action:
        case a.StartRun(rejoined=rejoined, drafted=drafted):
            asked = [False]

            def ask(head: str, draft: bool) -> tuple[bool, int]:
                asked[0] = True
                return merge.take(ln, take_turn, head, draft, drafted)

            updated, in_turn, in_line = merge._start_run(ln, ask, rejoined=rejoined)
            return a.RunStarted(
                updated, in_turn, in_line, asked[0], ln.tested_head, bool(ln.batched_s)
            )
        case a.ReadView():
            view = ln.view("state,mergeable,headRefOid")
            return a.Viewed(
                view.get("state", ""),
                view.get("mergeable") == "CONFLICTING",
                view.get("headRefOid") or "",
            )
        case a.CheckPolicy():
            ln.pinned_head = policy.check(ln)
            return a.PolicyChecked(ln.pinned_head)
        case a.ReadCi(head=head):
            checks: list[tuple[int, int]] = []
            try:
                rc, line = t.await_ci(head, 0, lambda d, n: checks.append((d, n)))
            except await_ci.RateLimitedError as exc:
                return a.CiRateLimited(exc.wait_s, exc.quota, str(exc))
            return a.CiRead(
                _CI.get(rc, Ci.OTHER),
                line,
                stall.of(line),
                checks[-1] if checks else None,
            )
        case a.WatchStall(watch=watch, head=head, stall=kind, waited=waited, line=line):
            return a.StallWatched(stall.on_poll(ln, watch, head, kind, waited, line))
        case a.RejoinLine(head=head, conflicting=conflicting):
            return a.Answer(stale_base.rejoin_line(ln, head, conflicting))
        case a.AskTurn(head=head, drafted=drafted):
            in_turn, in_line = merge.take(ln, take_turn, head, False, drafted)
            return a.TurnAnswered(in_turn, in_line, ln.tested_head)
        case a.RejoinInTurn(head=head, conflicting=conflicting):
            return a.Answer(stale_base.rejoin_in_turn(ln, head, conflicting))
        case a.RefuseBeforeMerge(head=head):
            slots.refuse_before_merge(ln, head)
            return a.Done()
        case a.GatePins(head=head):
            return a.PinsGated(tuple(pin_gate.gate_before_merge(ln, head)))
        case a.RecheckPins(pins=pins):
            pin_gate.recheck_before_merge(ln, list(pins))
            return a.Done()
        case a.ReadCommits():
            return a.CommitsRead(tuple(ln.view("commits").get("commits", [])))
        case a.ReadTested(head=head):
            interrupted, ln.turn_interrupted = ln.turn_interrupted, False
            return a.Tested(stale_base.batch_tested(ln, head), interrupted)
        case a.RefuseStaleOverlap(head=head):
            stale_base.refuse_stale_overlap(ln, head)
            return a.Done()
        case a.ReadTurnLost():
            return a.Answer(ln.turn_lost())
        case a.StepOut(reason=reason):
            ln.without_turn(lambda: None, reason)
            return a.Done()
        case a.Merge(head=head, commits=commits):
            return a.MergeAnswered(
                merge._merge_directly(
                    ln, head, cast(list[CommitMessage], list(commits))
                )
            )
        case a.FindFailing(head=head, refusal=refusal):
            return a.Names(tuple(failing_checks.find(ln, head, refusal)))
        case a.FindStuck(head=head):
            return a.Names(tuple(stuck_checks.find(ln, head)))
        case a.Sleep(seconds=seconds):
            t.sleep(seconds)
            return a.Slept(seconds)
        case a.WaitOutRateLimit(quota=quota, seconds=seconds):
            sleep = partial(
                gh_rate_limit.sleep_until_reset, quota, seconds, t.sleep, ln.read_quota
            )
            return a.Slept(ln.without_turn(sleep))
        case a.Say(text=text):
            say(text)
        case a.Enter(state=state):
            ln.transitions.enter(state)
        case a.Progress(done=done, total=total):
            ln.transitions.progress(done, total, CHECKS)
        case a.Die():
            ln.die(*merge.ending(str(ln.opts.pr), action))
        case a.Merged(waited=waited):
            merge.merged(ln, waited)
    return None


def record() -> None:
    """Write the corpus: each seed's scenario and the trace the loop produces for it.

    The file holds one block for each seed: a `# seed <n>` line, the scenario as one line of
    JSON, then one line for each line of the trace.
    """
    blocks = []
    for seed in SEEDS:
        sc = scenario(random.Random(seed))
        lines = [f"{_SEED}{seed}", json.dumps(asdict(sc)), *trace(sc, loop)]
        blocks.append("\n".join(lines))
    CORPUS.write_text("\n".join(blocks) + "\n")


def corpus() -> list[tuple[int, Scenario, list[str]]]:
    """The recorded seeds, each with its scenario and the trace the loop produced for it."""
    rows = []
    for block in CORPUS.read_text().split(_SEED)[1:]:
        seed, raw, *lines = block.rstrip("\n").split("\n")
        rows.append((int(seed), Scenario.parse(json.loads(raw)), lines))
    return rows
