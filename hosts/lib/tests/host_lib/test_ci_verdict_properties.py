"""A cancelled run never outvotes a run of the same name that decided, over generated heads.

A head is a list of check-runs drawn from the gate's real job names, spread over three suites so
that one name often carries several runs, as a draft run beside a ready run does. The property
tests take a fixed seed and no example database, so a CI run cannot differ from a local one.

Run: uv run pytest hosts/lib/tests/host_lib/test_ci_verdict_properties.py
"""

from collections.abc import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from ci_verdict import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CheckRun,
    CheckSuite,
    ci_verdict,
    jobs_verdict,
)

FIXED = settings(derandomize=True, database=None, deadline=None, max_examples=400)

_NAMES = sorted({CI_GATE_NAME, CI_PROVE_JOB, *CI_GATE_NEEDS_JOBS})
_REQUIRED = frozenset({CI_GATE_NAME})
_DECIDED = [("completed", c) for c in ("success", "neutral", "failure")]
_NO_VERDICT = [
    ("completed", c) for c in ("cancelled", "stale", "skipped_by_concurrency")
]
_OTHER = [("completed", "skipped"), ("in_progress", None), ("queued", None)]


def _runs(outcomes: Sequence[tuple[str, str | None]]) -> st.SearchStrategy[CheckRun]:
    return st.builds(
        lambda name, outcome, suite: CheckRun(name, *outcome, suite),
        st.sampled_from(_NAMES),
        st.sampled_from(outcomes),
        st.integers(1, 3),
    )


@st.composite
def _head_and_cancelled_run(draw) -> tuple[list[CheckRun], CheckRun]:
    """A head holding a decided run, and a no-verdict run of that run's name."""
    decided = draw(_runs(_DECIDED))
    rest = draw(st.lists(_runs(_DECIDED + _NO_VERDICT + _OTHER), max_size=30))
    head = draw(st.permutations([decided, *rest]))
    outcome = draw(st.sampled_from(_NO_VERDICT))
    return head, CheckRun(decided.name, *outcome, draw(st.integers(1, 3)))


@FIXED
@given(_head_and_cancelled_run())
def test_a_cancelled_run_beside_a_decided_one_leaves_the_verdict_alone(case):
    head, cancelled = case
    with_it = [*head, cancelled]

    assert ci_verdict(with_it, _REQUIRED) == ci_verdict(head, _REQUIRED)
    assert jobs_verdict(with_it) == jobs_verdict(head)


def _every_job(outcome: tuple[str, str | None], suite: int) -> list[CheckRun]:
    return [
        CheckRun(name, *outcome, suite)
        for name in (CI_GATE_NAME, CI_PROVE_JOB, *CI_GATE_NEEDS_JOBS)
    ]


def test_a_green_ready_run_passes_beside_its_cancelled_draft_run():
    """The shape that held PR 10278's landing for an hour: the draft run's jobs skipped or
    were cancelled, its gate was cancelled, and the ready run passed every job."""
    cancelled = {CI_GATE_NAME, "host_tools_arm64"}
    draft = [
        CheckRun(
            r.name, "completed", "cancelled" if r.name in cancelled else "skipped", 2
        )
        for r in _every_job(("completed", "skipped"), 2)
    ]
    head = [*_every_job(("completed", "success"), 1), *draft]

    assert ci_verdict(head, _REQUIRED) == "pass"


def test_a_proven_skip_beside_a_cancelled_run_is_still_pending():
    """A skipped job does not supersede a cancelled one: the draft run's skip reads like the
    proven path's skip, so `prove` passing must not turn the cancelled jobs into a pass."""
    prove = CheckRun(CI_PROVE_JOB, "completed", "success", 1)
    cancelled = [CheckRun(n, "completed", "cancelled", 1) for n in CI_GATE_NEEDS_JOBS]
    skipped = [CheckRun(n, "completed", "skipped", 2) for n in CI_GATE_NEEDS_JOBS]

    assert jobs_verdict([prove, *cancelled, *skipped]) == "pending"


def test_a_gate_failed_by_its_own_cancelled_run_stays_excused_beside_a_green_run():
    """The excuse reads the cancelled needs in the gate's own suite, so they are dropped only
    after it: dropping them first leaves the failed gate unexcused, and the head reads red."""
    cancelled_run = [
        *(CheckRun(n, "completed", "cancelled", 2) for n in CI_GATE_NEEDS_JOBS),
        CheckRun(CI_GATE_NAME, "completed", "failure", 2),
    ]
    suites = [
        CheckSuite.parse(
            {
                "id": 2,
                "status": "completed",
                "conclusion": "cancelled",
                "app": {"slug": "github-actions"},
            }
        )
    ]
    head = [*_every_job(("completed", "success"), 1), *cancelled_run]

    assert ci_verdict(head, _REQUIRED, lambda: suites) == "pass"


def test_the_generated_heads_reach_every_verdict():
    """Non-vacuity: a generator that only ever produced pending heads would pass the property."""
    seen: set[str] = set()

    @FIXED
    @given(_head_and_cancelled_run())
    def collect(case):
        seen.add(ci_verdict(case[0], _REQUIRED))

    collect()
    assert seen == {"pass", "pending", "fail"}
