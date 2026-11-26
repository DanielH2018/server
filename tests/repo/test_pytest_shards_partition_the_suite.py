"""CI's pytest matrix must run every test file exactly once, in exactly one shard.

`scripts/dev/pytest_shard.py` decides which test modules each matrix leg runs. Two
failures here are silent from the passing side: a file assigned to no shard is never run, and
CI still reports a green leg for every shard; a file assigned to two shards costs time and hides nothing,
but says the split is not a partition and the first failure is a coin flip away.

The workflow half matters as much as the helper. The legs split by `pytest_shard.CI_LEGS`,
and the matrix a run with no plan starts must name every one of them. This guard pins that,
because a fallback list of five beside a split of six would drop a sixth of the suite with
no error to read anywhere.

Every assignment here packs the committed runner-seconds table,
`scripts/dev/pytest_shard_runner_weights.json`. This module does not check whether a file is
missing from it: the docs-refresh cron fills those gaps from CI's logs. Balance is reported by
`pytest_shard.py --check-durations`,
run as a step of CI's own `pytest` job. It measures each module while running it, so it sees a
heavy unweighted file in a quiet directory and a stale recorded number, which a static check
cannot. This module keeps the partition properties, which are about correctness rather than
balance.

Run: uv run pytest tests/repo/test_pytest_shards_partition_the_suite.py
"""

import pytest
from dev import pytest_shard
from _helpers import REPO
from _workflow_rows import axis_values
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

KNOWN_TEST_FILES = frozenset(
    {
        "tests/repo/test_pytest_shards_partition_the_suite.py",
        "tests/repo/test_every_ci_job_is_gated.py",
        "scripts/validate/tests/test_render_k8s.py",
        "scripts/dev/tests/fanout/test_held_branches.py",
        ".claude/hooks/tests/bash_guards/test_block_protected_bash.py",
    }
)


def partition_problems(placed: dict[str, int], files, shards: int) -> list[str]:
    """What is wrong with an assignment, as a list of readable complaints.

    A function rather than a run of inline asserts, so the reject halves below can hand it a
    deliberately broken assignment. An assertion over the real census alone has no failing
    input, and a checker that returned `[]` unconditionally would pass every test.
    """
    problems = []
    expected = set(files)
    if missing := expected - set(placed):
        problems.append(f"assigned to no shard: {sorted(missing)}")
    if extra := set(placed) - expected:
        problems.append(f"assigned but not collected: {sorted(extra)}")
    if bad := {f: i for f, i in placed.items() if not 0 <= i < shards}:
        problems.append(f"outside 0..{shards - 1}: {sorted(bad)}")
    for shard in range(shards):
        if not any(i == shard for i in placed.values()):
            problems.append(f"shard {shard} is empty, so pytest would exit 5 on it")
    return problems


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 6, 8])
def test_every_test_file_lands_in_exactly_one_shard(shards):
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, shards)
    assert partition_problems(placed, files, shards) == []


@pytest.mark.parametrize("shards", [1, 3, 4])
def test_the_shards_reassemble_into_the_whole_census(shards):
    """The same property read the other way round: through the entry point CI calls, and by
    union rather than by membership, so a bug in `shard_files`'s 1-based indexing shows up."""
    files = pytest_shard.census()
    union = []
    for shard in range(1, shards + 1):
        union += pytest_shard.shard_files(shard, shards, files=files)
    assert sorted(union) == files
    assert len(union) == len(set(union)), "a file was handed to more than one shard"


def test_the_census_finds_the_files_it_must_find():
    """Non-vacuity. An empty census makes every check above true and covers nothing."""
    census = set(pytest_shard.census())
    assert KNOWN_TEST_FILES <= census, (
        f"pytest_shard.census() no longer finds {sorted(KNOWN_TEST_FILES - census)}"
    )


def test_the_assignment_is_deterministic():
    """Two legs of the matrix compute the split independently on separate runners. They agree
    only if the same census and weights always give the same answer."""
    files = pytest_shard.census()
    assert pytest_shard.assign(files, 4) == pytest_shard.assign(files, 4)


def test_a_moved_file_still_lands_in_exactly_one_shard():
    """A file whose path changes is a file with no recorded weight. It must still be placed —
    at its directory's median, not a KeyError and not a silent drop."""
    files = pytest_shard.census()
    moved = files[:-1] + ["tests/repo/test_moved_somewhere_new.py"]
    placed = pytest_shard.assign(moved, 4)
    assert partition_problems(placed, moved, 4) == []
    assert "tests/repo/test_moved_somewhere_new.py" in placed


def test_a_dropped_file_is_flagged():
    """The reject half. Without it, a checker that always returned no problems would pass."""
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, 4)
    del placed[files[0]]
    assert partition_problems(placed, files, 4) == [
        f"assigned to no shard: {[files[0]]}"
    ]


def test_an_out_of_range_shard_is_flagged():
    """The second reject half: a file placed in a shard the matrix does not run."""
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, 4)
    placed[files[0]] = 9
    assert any(
        "outside 0..3" in problem for problem in partition_problems(placed, files, 4)
    )


def test_an_empty_shard_is_flagged():
    """The third: more shards than files leaves a leg collecting nothing, which pytest exits 5
    on rather than passing."""
    files = pytest_shard.census()[:2]
    placed = pytest_shard.assign(files, 4)
    assert any(
        "is empty" in problem for problem in partition_problems(placed, files, 4)
    )


def _pytest_job() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest"]


def _plan_job() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest_plan"]


def test_a_run_with_no_plan_starts_every_leg_the_split_fills():
    """`--shard` is 1-based and `shard_files` rejects anything outside 1..N. A fallback list
    shorter than `CI_LEGS` leaves the last legs' modules unrun on every push and dispatch."""
    shards = axis_values(_pytest_job()["strategy"]["matrix"]["shard"])
    assert shards == list(range(1, pytest_shard.CI_LEGS + 1)), (
        f"ci.yml's fallback shard list is {shards}"
    )


def test_the_workflow_measures_every_shard_and_checks_what_it_measured():
    """The measured coverage gate is a workflow step, so nothing in the suite makes it run.

    Three things have to hold together or the gate is green over nothing: the test step asks
    for every duration, it keeps the report, and a later step reads that same report. A step
    dropped or a path typed differently leaves `--check-durations` reading an absent file --
    which fails loudly -- or, worse, leaves the gate out of the job entirely with every leg
    still passing. It is a `run:` step rather than a pytest test because only the leg that ran
    a module has its measurement.
    """
    runs = [str(s.get("run", "")) for s in _pytest_job()["steps"]]
    suite = [r for r in runs if "-m pytest" in r]
    assert len(suite) == 1, "expected exactly one step to run the suite"
    assert "--durations=0" in suite[0], (
        "the gate sums per-file durations, which a truncated --durations=N cannot support"
    )
    log = "$RUNNER_TEMP/durations.log"
    assert log in suite[0], f"the test step must keep its report at {log}"
    gates = [r for r in runs if "--check-durations" in r]
    assert len(gates) == 1, "expected exactly one step to run the coverage gate"
    assert log in gates[0], (
        f"the gate must read the report the test step wrote at {log}"
    )


def test_every_leg_splits_by_the_count_the_plan_and_the_fallback_use():
    """A literal `--of 4` beside a six-leg matrix silently drops or double-runs part of the
    suite, and every leg still reports green. The legs and the plan take the default."""
    steps = _pytest_job()["steps"] + _plan_job()["steps"]
    runs = [
        str(s.get("run", "")) for s in steps if "pytest_shard.py" in str(s.get("run"))
    ]
    assert len(runs) == 3, runs
    assert not [r for r in runs if "--of" in r]
