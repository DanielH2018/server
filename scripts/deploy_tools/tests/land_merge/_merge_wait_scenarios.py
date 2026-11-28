"""Merge-wait scenarios: what every boundary of one merge wait answers, drawn from a seed.

A scenario is the answers every boundary of one merge wait gives: the PR's polls, the CI reads,
the turn, the merge call and the reads around it. `scenario` draws one from a seeded `Random`,
so a seed names a scenario for good. `landing` builds the `Landing` over `_land_fakes` that
answers as a scenario says.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from _land_fakes import MERGE_SHA, Fakes, make_landing
from deploy_tools import await_ci, ci_stall
from deploy_tools.land_lib.landing import Landing
from lib.exit_codes import CI_DISARMED, CI_GREEN, CI_PENDING, CI_RED

# A wait that reads GitHub this often is a loop that never sleeps, not a slow scenario.
MAX_READS = 2000

HEAD = "a" * 10
MOVED = "b" * 10
_CI_LINES = (
    "CI green",
    "CI pending",
    "2 checks still running",
    f"pending: {ci_stall.DRAFT_SKIPPED}",
    f"pending: {ci_stall.NO_RUN_LEFT}",
    "prek failed",
)
_REQUIRED = ["prek (lint + validate + tests + secrets)", "renovate config validator"]
_REFUSALS = (
    "boom",
    'Required status check "renovate config validator" is failing.',
    "2 of 2 required status checks are in progress.",
)
_RUNS = (
    [{"id": 1, "status": "completed", "conclusion": "skipped"}],
    [{"id": 2, "status": "in_progress", "conclusion": None}],
    [
        {"id": 3, "status": "completed", "conclusion": "cancelled"},
        {"id": 1, "status": "completed", "conclusion": "skipped"},
    ],
    [{"id": 4, "status": "completed", "conclusion": "cancelled"}],
)
_SUITE = 116713431402
_CHECK_RUNS = (
    [],
    [{"name": "pull images", "status": "queued", "check_suite": {"id": _SUITE}}],
    [
        {"name": _REQUIRED[0], "status": "completed", "conclusion": "success"},
        {"name": _REQUIRED[1], "status": "completed", "conclusion": "failure"},
    ],
)


class TurnBy(StrEnum):
    """Who offers a merge wait the landing turn, as each caller of `merge.await_merge` does."""

    # `turn.held`, as the pipeline offers it to a landing that forms no batch.
    HELD = "held"
    # No turn: `merge._no_turn`.
    NONE = "none"
    # `batch.forming` over `turn.held`, as `batch.land` offers it.
    BATCH = "batch"
    # A batch member's wait, inside the turn its holder holds (`batch._merge_member`).
    MEMBER = "member"


@dataclass(frozen=True)
class Scenario:
    """What every boundary of one merge wait answers, in a shape JSON holds.

    Attributes:
        polls: Each poll's `state`, `mergeable` and head, the last repeating.
        start_draft: Whether the PR is a draft when the wait first reads it.
        ci: Each CI read's exit code and line; a code of -1 is a read GitHub refused for its
            rate, the line then naming the seconds until the quota frees.
        turn: What each try for the landing turn answers.
        merge_rc: The exit code of each `gh` call, merges and branch updates alike.
        refusal: The stderr of a `gh` call that fails.
        compare: Whether master moved on a path the PR leaves alone, per compare read.
        runs: Which of `_RUNS` the head's `ci.yml` runs are.
        check_runs: Which of `_CHECK_RUNS` the head's check-runs are.
        turn_lost: For a wait with no turn to offer, what each `ln.turn_lost` answers.
        merge_timeout: `--merge-timeout`.
        merge_poll: `--merge-poll`.
        direct: Whether this landing merges the PR itself.
        turn_by: Who offers the wait the landing turn (`TurnBy`).
        pinned: Whether the landing policy pinned the first head.
        tested: Whether a landing batch tested the first head on master's tip.
        batched_s: The seconds of the batch's run the wait counts.
        interrupted: Whether a wait out of the turn came before this one.
    """

    polls: list[tuple[str, str, str]]
    start_draft: bool = False
    ci: list[tuple[int, str]] = field(default_factory=lambda: [(CI_GREEN, "CI green")])
    turn: list[bool] = field(default_factory=lambda: [True])
    merge_rc: list[int] = field(default_factory=lambda: [0])
    refusal: str = "boom"
    compare: list[bool] = field(default_factory=lambda: [False])
    runs: int = 0
    check_runs: int = 0
    turn_lost: list[bool] = field(default_factory=lambda: [False])
    merge_timeout: int = 300
    merge_poll: int = 30
    direct: bool = True
    turn_by: TurnBy = TurnBy.HELD
    pinned: bool = False
    tested: bool = False
    batched_s: int = 0
    interrupted: bool = False

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Scenario":
        """The scenario `dataclasses.asdict` wrote as JSON."""
        fields: dict[str, Any] = dict(raw)
        fields["polls"] = [tuple(p) for p in raw["polls"]]
        fields["ci"] = [tuple(c) for c in raw["ci"]]
        fields["turn_by"] = TurnBy(raw["turn_by"])
        return cls(**fields)


def scenario(rng: random.Random) -> Scenario:
    """One scenario, every choice drawn from `rng`.

    Most draw every answer at random. Three kinds a random draw rarely reaches get a share of
    their own: a pinned head the landing's own branch update moves, a green head GitHub
    refuses to merge until its checks are read for one that never finishes, and a head a
    batch tested that reaches its merge after a wait out of the turn.
    """

    def some[T](choices: list[T] | tuple[T, ...], most: int) -> list[T]:
        return [rng.choice(choices) for _ in range(rng.randint(1, most))]

    polls = some(
        [
            ("OPEN", "MERGEABLE", HEAD),
            ("OPEN", "MERGEABLE", HEAD),
            ("OPEN", "UNKNOWN", HEAD),
            ("OPEN", "CONFLICTING", HEAD),
            ("OPEN", "MERGEABLE", MOVED),
            ("OPEN", "MERGEABLE", ""),
        ],
        8,
    )
    merge_rc = some([0, 0, 1], 4)
    lost = some([False, False, True], 3)
    # A wait whose PR never reads merged ends only on its budget, so the merge it retries on
    # every poll is refused, and a lost turn is found again only finitely often.
    if rng.random() < 0.7:
        polls.append((rng.choice(["MERGED", "MERGED", "CLOSED"]), "UNKNOWN", HEAD))
    else:
        merge_rc.append(1)
    lost.append(False)
    ci: list[tuple[int, str]] = []
    for _ in range(rng.randint(1, 6)):
        code = rng.choice(
            [CI_GREEN, CI_GREEN, CI_PENDING, CI_PENDING, CI_RED, CI_DISARMED, 9, -1]
        )
        line = str(rng.choice([30, 900])) if code == -1 else rng.choice(_CI_LINES)
        ci.append((code, line))
    drawn = Scenario(
        polls=polls,
        start_draft=rng.random() < 0.4,
        ci=ci,
        turn=some([True, True, False], 3),
        merge_rc=merge_rc,
        refusal=rng.choice(_REFUSALS),
        compare=some([False, True], 3),
        runs=rng.randrange(len(_RUNS)),
        check_runs=rng.randrange(len(_CHECK_RUNS)),
        turn_lost=lost,
        merge_timeout=rng.choice([60, 300, 1800]),
        merge_poll=rng.choice([30, 30, 300]),
        direct=rng.random() < 0.85,
        turn_by=rng.choice(
            [*[TurnBy.HELD] * 3, *[TurnBy.NONE, TurnBy.BATCH, TurnBy.MEMBER] * 2]
        ),
        tested=rng.random() < 0.25,
        batched_s=rng.choice([0, 0, 120]),
        interrupted=rng.random() < 0.2,
    )
    kind = rng.random()
    if kind < 0.25:
        # The one head move a pinned landing survives is its own branch update: a draft with
        # no run, in the turn, behind a master that moved.
        return replace(
            drawn,
            polls=[("OPEN", "MERGEABLE", HEAD), ("OPEN", "MERGEABLE", MOVED), *polls],
            merge_rc=[0, *merge_rc],
            start_draft=True,
            turn=[True],
            compare=[True],
            runs=0,
            turn_by=TurnBy.HELD,
            pinned=True,
        )
    if kind < 0.35:
        return replace(
            drawn,
            polls=[("OPEN", "MERGEABLE", HEAD)],
            ci=[(CI_GREEN, "CI green")],
            merge_rc=[1],
            refusal="boom",
            start_draft=False,
            runs=1,
            check_runs=1,
            merge_timeout=1800,
            merge_poll=300,
        )
    if kind < 0.45:
        # A head a batch tested on master's tip reaches its merge, some after a wait out of
        # the turn: whether that wait throws the batch's proof away.
        return replace(
            drawn,
            polls=[("OPEN", "MERGEABLE", HEAD), ("MERGED", "UNKNOWN", HEAD)],
            ci=[(CI_GREEN, "CI green")],
            merge_rc=[0],
            compare=[False],
            turn=[True],
            turn_by=TurnBy.HELD,
            direct=True,
            tested=True,
            interrupted=rng.random() < 0.7,
        )
    if drawn.turn_by is TurnBy.MEMBER:
        # A holder stacks a member only while it holds the turn.
        return replace(drawn, turn=[True])
    return drawn


class _CiRead:
    """The fake await_ci: one entry per read, the last repeating; -1 refuses for the rate."""

    def __init__(self, answers: list[tuple[int, str]]) -> None:
        self._answers = answers
        self._at = 0

    def __call__(self, sha: str, timeout: int, on_checks: Callable[[int, int], None]):
        code, line = self._answers[min(self._at, len(self._answers) - 1)]
        self._at += 1
        if code == -1:
            raise await_ci.RateLimitedError(int(line))
        on_checks(1, 3)
        return code, f"{sha[:8]}: {line}"


def landing(sc: Scenario) -> tuple[Landing, list]:
    view = [{"state": s, "mergeable": mg, "headRefOid": h} for s, mg, h in sc.polls]
    first = view[0]
    fakes = Fakes(
        gh_views={
            "state,mergeable,headRefOid": view,
            "isDraft,state,mergeable,headRefOid": [
                {"isDraft": sc.start_draft, **first}
            ],
            "isDraft": [{"isDraft": sc.start_draft}],
            "headRefOid": [{"headRefOid": MOVED}],
            "headRefOid,headRefName,baseRefName,isCrossRepository": [
                {
                    "headRefOid": MOVED,
                    "headRefName": "claude/land-it",
                    "baseRefName": "master",
                    "isCrossRepository": False,
                }
            ],
        },
        turn=list(sc.turn),
        gh_merge_rc=list(sc.merge_rc),
        gh_stderr=sc.refusal,
        compare=[["docs/other.md"] if moved else [] for moved in sc.compare],
        head_runs=_RUNS[sc.runs],
        check_runs=_CHECK_RUNS[sc.check_runs],
        completed_suites=[
            {"id": _SUITE, "status": "completed", "conclusion": "failure"}
        ],
        required_contexts=_REQUIRED,
    )
    ln, calls = make_landing(
        fakes, merge_timeout=sc.merge_timeout, merge_poll=sc.merge_poll
    )
    reads = [0]
    gh_json = ln.tools.gh_json

    def bounded(*args, **kwargs):
        reads[0] += 1
        if reads[0] > MAX_READS:
            raise RuntimeError(f"more than {MAX_READS} GitHub reads")
        if args[1].endswith("/git/ref/heads/master"):
            calls.append(("gh:master", (), {}))
            return {"sha": MERGE_SHA}
        return gh_json(*args, **kwargs)

    ln.tools = replace(ln.tools, gh_json=bounded, await_ci=_CiRead(sc.ci))
    arm(ln, sc)
    return ln, calls


def arm(ln: Landing, sc: Scenario) -> None:
    """Set the fields of `ln` that `sc` names, as the landing has them when its wait starts."""
    if sc.direct:
        ln.direct_merge_subject = "Land it"
    if sc.pinned:
        ln.pinned_head = HEAD
    if sc.tested:
        ln.tested_head, ln.tested_on = HEAD, MERGE_SHA
    ln.batched_s = sc.batched_s
    ln.turn_interrupted = sc.interrupted
    if sc.turn_by is TurnBy.NONE:
        answers = iter(sc.turn_lost)
        last = [False]

        def lost() -> bool:
            last[0] = next(answers, last[0])
            return last[0]

        ln.turn_lost = lost
