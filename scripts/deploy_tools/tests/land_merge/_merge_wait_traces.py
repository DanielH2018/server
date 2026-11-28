"""The call trace a merge wait produces for a scenario, and the actions its driver performs.

`trace` runs `merge.await_merge` for a scenario (`_merge_wait_scenarios`) and returns what the
wait did, as text: every boundary call in order, every printed line, and how the wait ended.

`performed` records what the driver did inside a `trace`: for each merge wait, the state it
started from, each action it carried out and each answer it built. `modelled` runs
`core/landing_machine.step` alone over those answers, with no fakes and no driver.
"""

import contextlib
import io
import json
import random
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from unittest import mock

from _land_fakes import PRIMARY
from _merge_wait_scenarios import Scenario, TurnBy, arm_member, landing, scenario
from deploy_tools.land_lib import batch, merge, turn
from deploy_tools.land_lib.core import landing_actions as a
from deploy_tools.land_lib.core import landing_machine
from deploy_tools.land_lib.core.landing_state import LandingState
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome

CORPUS = Path(__file__).with_name("merge_wait_traces.txt")
_SEED = "# seed "
# The seeds the corpus records, one scenario each.
SEEDS = range(200)
# The PR a batch member's landing lands, beside the holder's.
MEMBER_PR = "1000"


def _line(name: str, args: tuple, kwargs: dict) -> str | None:
    """One boundary call as trace text, or None for a call the trace leaves out."""
    if name == "record":
        # The state record names the pid, and the transition line beside it holds the same move.
        return None
    words = [name, *(json.dumps(arg, default=str) for arg in args)]
    words += [f"{k}={json.dumps(v, default=str)}" for k, v in kwargs.items() if v]
    return " ".join(words).replace(str(PRIMARY), "<primary>")


class _Printed(io.TextIOBase):
    """A stdout that records each printed line into `calls`, between the calls around it."""

    def __init__(self, calls: list) -> None:
        self._calls = calls
        self._part = ""

    def write(self, text: str) -> int:
        *lines, self._part = (self._part + text).split("\n")
        self._calls.extend(("say", (line,), {}) for line in lines)
        return len(text)


def _offered(ln: Landing, sc: Scenario) -> None:
    """Run the merge wait with the turn offered as `sc.turn_by` says, as that caller does."""
    if sc.turn_by is TurnBy.NONE:
        merge.await_merge(ln)
        return
    with turn.held(ln) as take_turn:
        if sc.turn_by is TurnBy.HELD:
            merge.await_merge(ln, take_turn)
        elif sc.turn_by is TurnBy.BATCH:
            merge.await_merge(ln, batch.forming(ln, take_turn))
        else:
            take_turn("")
            member = batch._member(ln, MEMBER_PR)
            arm_member(member, sc)
            merge.await_merge(member, lambda _draft: (True, 0))


def trace(sc: Scenario) -> list[str]:
    """What the merge wait does over the fakes `sc` describes, as lines of text."""
    ln, calls = landing(sc)
    with contextlib.redirect_stdout(_Printed(calls)):
        try:
            _offered(ln, sc)
            end = "merged"
        except Outcome as exc:
            end = f"outcome rc={exc.rc} verdict={exc.verdict} error={exc.error}"
        except Exception as exc:
            end = f"raised {type(exc).__name__}: {exc}"
    return [*(line for c in calls if (line := _line(*c))), end]


@dataclass
class Run:
    """One merge wait as its driver ran it.

    Attributes:
        first: The state the wait started from.
        actions: Each action the driver began to carry out, in order.
        answers: The answer to each action that returned, in the same order. An action that
            ended the landing has none, and is the last.
    """

    first: LandingState
    actions: list[a.Action] = field(default_factory=list)
    answers: list[a.Observation] = field(default_factory=list)


@contextlib.contextmanager
def performed() -> Generator[list[Run]]:
    """Record every merge wait the driver runs inside the block, in the order they start.

    A batch holder's wait runs each member's wait inside its own ask for the turn, so the
    waits nest.
    """
    runs: list[Run] = []
    live: list[Run] = []
    drive, perform = merge._drive, merge._perform

    def driven(ln: Landing, take_turn: merge.TakeTurn, first: LandingState) -> None:
        run = Run(first)
        runs.append(run)
        live.append(run)
        try:
            drive(ln, take_turn, first)
        finally:
            live.pop()

    def carried_out(
        ln: Landing, take_turn: merge.TakeTurn, action: a.Action
    ) -> a.Observation:
        run = live[-1]
        run.actions.append(action)
        answer = perform(ln, take_turn, action)
        run.answers.append(answer)
        return answer

    with (
        mock.patch.object(merge, "_drive", driven),
        mock.patch.object(merge, "_perform", carried_out),
    ):
        yield runs


def modelled(run: Run) -> list[a.Action]:
    """The actions `step` returns for `run`'s answers, up to the first it has no answer for."""
    s, todo = landing_machine.start(run.first)
    actions = [*todo]
    while len(actions) <= len(run.answers) and not isinstance(todo[-1], a.ENDS):
        s, todo = landing_machine.step(s, run.answers[len(actions) - 1])
        actions += todo
    return actions


def record() -> None:
    """Write the corpus: each seed's scenario and the trace the merge wait produces for it.

    The file holds one block for each seed: a `# seed <n>` line, the scenario as one line of
    JSON, then one line for each line of the trace.
    """
    blocks = []
    for seed in SEEDS:
        sc = scenario(random.Random(seed))
        lines = [f"{_SEED}{seed}", json.dumps(asdict(sc)), *trace(sc)]
        blocks.append("\n".join(lines))
    CORPUS.write_text("\n".join(blocks) + "\n")


def corpus() -> list[tuple[int, Scenario, list[str]]]:
    """The recorded seeds, each with its scenario and the trace recorded for it."""
    rows = []
    for block in CORPUS.read_text().split(_SEED)[1:]:
        seed, raw, *lines = block.rstrip("\n").split("\n")
        rows.append((int(seed), Scenario.parse(json.loads(raw)), lines))
    return rows
