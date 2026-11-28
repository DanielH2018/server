"""A landing's state, written at each change: a state record on the turn host and a syslog line.

`State` names the states of *The landing state machine* in `docs/landing-architecture.md`.
`Transitions` is the one writer, and each `Landing` owns one. Nothing in the landing decides
on what it writes.

Each state change writes two records:

- **A state record**, `<pr>.<uid>.state` in the line's directory on `TURN_HOST`, replaced by
  a rename (`line.py`'s `record` operation). A landing on another host writes it through ssh, as
  it writes its place in the line. It is one JSON object: `pr`, `host`, `pid`, `state`,
  `since` (epoch seconds), `history` (each state before it, with its `since`), `progress`
  (`done`, `total` and `what`, or null) and `verdict`. The record stays after the verdict and
  names it. A record whose `pid` is gone and whose state is not `verdict` is a landing that
  died. A PR landed by two users has a record from each, and the newest `since` is the live
  one.
- **A transition line**, logfmt through `Tools.logger`:
  `event=land_transition pr=<n> from=<state> to=<state> at=<epoch seconds> verdict=<v>`.
  `from` is empty for the first state, and only the line into `verdict` names a verdict.
  The event is not named `landing...`, because the Landings board selects its lines with
  `|= "event=landing"`, which would count each transition as a landing.

`progress` rewrites the state record alone, and only when the triple changes.

Neither write ever fails a landing. The first state record that fails to write stops the
record for the rest of the landing, so a turn host that does not answer costs one ssh timeout,
not one per state. The transition lines go on.
"""

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from deploy_tools.land_lib import line
from deploy_tools.land_lib.outcome import say

from lib import host_modules  # noqa: F401
from host_lib import logfmt

EVENT = "land_transition"
# What a count of progress counts: a CI read's check-runs, `land_wait`'s Kustomizations.
CHECKS = "checks finished"
KUSTOMIZATIONS = "Kustomizations ready"


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


def transition_line(pr: str, before: str, after: str, at: float, verdict: str) -> str:
    """The logfmt line for a move from `before` ("" for none) to `after` at `at` epoch seconds."""
    fields = {
        "event": EVENT,
        "pr": pr or "unknown",
        "from": before,
        "to": after,
        "at": f"{at:.3f}",
        "verdict": verdict,
    }
    return logfmt(fields)


@dataclass(frozen=True)
class Progress:
    """How far a landing is through its state: `done` of `total` `what`."""

    done: int
    total: int
    what: str


class Boundaries(Protocol):
    """The fields of `Tools` the writer uses, named here so this module imports no `Tools`."""

    now: Callable[[], float]
    logger: Callable[[str], None]
    hostname: Callable[[], str]
    line: Callable[..., str] | None


class Transitions:
    """The states one landing has entered, and the writer of both records at each change.

    Args:
        pr: The PR the landing lands.
        tools: The landing's `Tools`. None writes nothing, for a batch member, whose own
            landing writes its PR's records.
    """

    def __init__(self, pr: str, tools: Boundaries | None) -> None:
        self._pr = pr
        self._tools = tools
        self._host = tools.hostname() if tools else ""
        self._line = (
            None if tools is None else tools.line or line.at_turn_host(self._host)
        )
        self.state: State | None = None
        self._since = 0.0
        self._history: list[tuple[State, float]] = []
        self._progress: Progress | None = None
        self._verdict = ""

    def enter(self, state: State) -> None:
        """Move into `state`, unless the landing is already in it or has its verdict."""
        self._move(state, "")

    def end(self, verdict: str) -> None:
        """Move into `State.VERDICT`, naming `verdict`. Later calls change nothing."""
        self._move(State.VERDICT, verdict)

    def progress(self, done: int, total: int, what: str) -> None:
        """Record how far the landing is through its state; a new state clears it."""
        now = Progress(done, total, what)
        if self._tools is None or now == self._progress:
            return
        self._progress = now
        self._write()

    def counter(self, what: str) -> Callable[[int, int], None]:
        """`progress` for `what`, as the `(done, total)` callback a read takes."""
        return lambda done, total: self.progress(done, total, what)

    def _move(self, state: State, verdict: str) -> None:
        tools = self._tools
        if tools is None or state is self.state or self.state is State.VERDICT:
            return
        before = self.state
        if before is not None:
            self._history.append((before, self._since))
        self.state, self._since = state, tools.now()
        self._progress, self._verdict = None, verdict
        text = transition_line(self._pr, before or "", state, self._since, verdict)
        with contextlib.suppress(Exception):
            tools.logger(text)
        self._write()

    def _write(self) -> None:
        if self._line is None or self.state is None:
            return
        record = {
            "pr": self._pr,
            "host": self._host,
            "pid": os.getpid(),
            "state": self.state,
            "since": self._since,
            "history": [{"state": s, "since": t} for s, t in self._history],
            "progress": asdict(self._progress) if self._progress else None,
            "verdict": self._verdict,
        }
        try:
            self._line("record", self._pr, json.dumps(record))
        except Exception as exc:
            say(
                f"could not write PR #{self._pr}'s state record ({exc}); its states go "
                "to syslog only"
            )
            self._line = None
