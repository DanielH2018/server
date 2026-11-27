"""Properties of `ci_verdict` and `jobs_verdict` over generated heads.

A head is a list of check-runs drawn from the gate's real job names, spread over three suites so
that one name often carries several runs, as a draft run beside a ready run does. A cancelled run
never outvotes a run of the same name that decided. `jobs_verdict` ignores run order, reads any
failed need as `fail`, and passes only a head whose every planned need passed or was excused.
The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest hosts/lib/tests/host_lib/test_ci_verdict_properties.py
"""

from collections import defaultdict
from collections.abc import Sequence

from hypothesis import given
from hypothesis import strategies as st

from ci_verdict import (
    CI_BUILD_JOB,
    CI_BUILD_READER_JOBS,
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CI_PYTEST_LEGS,
    CI_SKIPPED_MATRIX_JOBS,
    CheckRun,
    CheckSuite,
    RunState,
    ci_verdict,
    jobs_verdict,
)


_NAMES = sorted(
    {CI_GATE_NAME, CI_PROVE_JOB, *CI_GATE_NEEDS_JOBS, *CI_SKIPPED_MATRIX_JOBS}
)
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

    @given(_head_and_cancelled_run())
    def collect(case):
        seen.add(ci_verdict(case[0], _REQUIRED))

    collect()
    assert seen == {"pass", "pending", "fail"}


_SUCCESS = ("completed", "success")
_SKIPPED = ("completed", "skipped")
_PYTEST_MATRIX_SKIP = next(
    name for name, legs in CI_SKIPPED_MATRIX_JOBS.items() if legs == CI_PYTEST_LEGS
)
_NOISE = [*_DECIDED, *_NO_VERDICT, *_OTHER, _SUCCESS, _SKIPPED]


@st.composite
def _heads(draw) -> list[CheckRun]:
    """A ran or a proven head, with some of its runs dropped and up to four noise runs added.

    A ran head has every need succeed, only the first k pytest legs, and may skip a build
    reader. A proven head has `prove` succeed and every need skip, its legs as one matrix-level
    skip or as their own runs.
    """
    proven = draw(st.booleans())
    need = _SKIPPED if proven else _SUCCESS
    status, conclusion = (
        _SUCCESS if proven else draw(st.sampled_from(_DECIDED + _NO_VERDICT))
    )
    base = [CheckRun(CI_PROVE_JOB, status, conclusion, 1)]
    for name in sorted(CI_GATE_NEEDS_JOBS - CI_PYTEST_LEGS):
        reader_skips = name in CI_BUILD_READER_JOBS and draw(st.booleans())
        base.append(CheckRun(name, *(_SKIPPED if reader_skips else need), 1))
    if proven and draw(st.booleans()):
        base.append(CheckRun(_PYTEST_MATRIX_SKIP, *_SKIPPED, 1))
    else:
        legs = sorted(CI_PYTEST_LEGS)[: draw(st.integers(1, len(CI_PYTEST_LEGS)))]
        base.extend(CheckRun(leg, *need, 1) for leg in legs)
    kept = [run for run in base if draw(st.integers(0, 9))]
    noise = draw(st.lists(_runs(_NOISE), max_size=4))
    return draw(st.permutations([*kept, *noise]))


def _states(head: list[CheckRun]) -> defaultdict[str, set[RunState]]:
    """Each name's run states, a matrix-level skip counted as a skip of each of its legs."""
    states = defaultdict(set)
    for run in head:
        states[run.name].add(run.state)
        if run.state is RunState.SKIPPED:
            for leg in CI_SKIPPED_MATRIX_JOBS.get(run.name, ()):
                states[leg].add(RunState.SKIPPED)
    return states


def _planned_needs(head: list[CheckRun]) -> list[str]:
    """Every need but the pytest legs numbered past the highest leg with a run of its own."""
    own_legs = sorted(run.name for run in head if run.name in CI_PYTEST_LEGS)
    last = own_legs[-1] if own_legs else max(CI_PYTEST_LEGS)
    return [n for n in CI_GATE_NEEDS_JOBS if n not in CI_PYTEST_LEGS or n <= last]


def _unexcused_needs(head: list[CheckRun]) -> list[str]:
    """The planned needs with neither a passing run nor an excused skip."""
    states = _states(head)
    proven = RunState.PASS in states[CI_PROVE_JOB] and not any(
        RunState.PASS in states[n] for n in CI_GATE_NEEDS_JOBS
    )
    built = RunState.PASS in states[CI_BUILD_JOB]
    return [
        name
        for name in _planned_needs(head)
        if RunState.PASS not in states[name]
        and not (
            RunState.SKIPPED in states[name]
            and (proven or (built and name in CI_BUILD_READER_JOBS))
        )
    ]


@given(_heads(), st.data())
def test_the_order_of_a_heads_runs_does_not_change_its_verdict(head, data):
    reordered = data.draw(st.permutations(head))

    assert jobs_verdict(reordered) == jobs_verdict(head)


@given(_heads(), st.sampled_from(sorted(CI_GATE_NEEDS_JOBS)), st.integers(1, 3))
def test_a_failed_need_fails_the_head(head, name, suite):
    """`prove` is not a need: its failure is a runner fault, after which every need runs."""
    failed = CheckRun(name, "completed", "failure", suite)

    assert jobs_verdict([*head, failed]) == "fail"


@given(_heads())
def test_a_passing_head_has_every_planned_need_passed_or_excused(head):
    if jobs_verdict(head) == "pass":
        assert _unexcused_needs(head) == []


def _pass_routes(head: list[CheckRun]) -> set[str]:
    states = _states(head)
    routes = set()
    if not any(RunState.PASS in states[n] for n in CI_GATE_NEEDS_JOBS):
        routes.add("proven")
    if any(states[n] == {RunState.SKIPPED} for n in CI_BUILD_READER_JOBS):
        routes.add("reader skipped beside a build")
    if len(_planned_needs(head)) < len(CI_GATE_NEEDS_JOBS):
        routes.add("legs pruned")
    if any(run.name == _PYTEST_MATRIX_SKIP for run in head):
        routes.add("legs skipped at matrix level")
    return routes


def test_the_structured_heads_reach_every_verdict_and_every_pass_route():
    """Non-vacuity: the completeness property reads only the heads that pass."""
    verdicts: set[str] = set()
    routes: set[str] = set()

    @given(_heads())
    def collect(head):
        verdict = jobs_verdict(head)
        verdicts.add(verdict)
        if verdict == "pass":
            routes.update(_pass_routes(head))

    collect()
    assert verdicts == {"pass", "pending", "fail"}
    assert routes == {
        "proven",
        "reader skipped beside a build",
        "legs pruned",
        "legs skipped at matrix level",
    }
