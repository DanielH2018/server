"""CI's pytest matrix must run every test file exactly once, in exactly one shard.

`scripts/dev/pytest_shard.py` decides which test modules each matrix leg runs (#1270). Two
failures here are silent from the passing side: a file assigned to no shard is never run, and
CI still reports four green legs; a file assigned to two shards costs time and hides nothing,
but says the split is not a partition and the first failure is a coin flip away.

The workflow half matters as much as the helper. The `pytest` job passes `strategy.job-total`
as `--of`, so the split follows the matrix by construction — this guard pins that, because a
hardcoded `--of 4` beside a five-entry matrix would drop a fifth of the suite with no error to
read anywhere.

Run: uv run pytest ansible/tests/repo/test_pytest_shards_partition_the_suite.py
"""

import re

import pytest
import pytest_shard
from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# Non-vacuity, per the repo rule that a check finding its own subject by pattern ships with a
# named member it must find. `census()` globs `git ls-files` under `testpaths`; a rename, a moved
# directory or a broken glob would return an EMPTY set, and every `all(...)` below would pass
# over nothing. These five span four different testpath roots on purpose.
KNOWN_TEST_FILES = frozenset(
    {
        "ansible/tests/repo/test_pytest_shards_partition_the_suite.py",
        "ansible/tests/repo/test_every_ci_job_is_gated.py",
        "scripts/validate/tests/test_validate_k8s_manifests.py",
        "scripts/dev/tests/test_prune_worktrees.py",
        ".claude/hooks/tests/test_block_protected_bash.py",
    }
)

# How stale the recorded weights may get before the balance is guesswork rather than measurement.
# Unknown files are given the median weight, so exceeding this costs balance and never
# correctness — re-record with `uv run python scripts/dev/pytest_shard.py --record`.
MAX_UNWEIGHTED_FRACTION = 0.2


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


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 8])
def test_every_test_file_lands_in_exactly_one_shard(shards):
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, shards, pytest_shard.load_weights())
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
    files, weights = pytest_shard.census(), pytest_shard.load_weights()
    assert pytest_shard.assign(files, 4, weights) == pytest_shard.assign(
        files, 4, weights
    )


def test_a_moved_file_still_lands_in_exactly_one_shard():
    """A file whose path changes is a file with no recorded weight. It must still be placed —
    the median fallback, not a KeyError and not a silent drop."""
    files = pytest_shard.census()
    moved = files[:-1] + ["ansible/tests/repo/test_moved_somewhere_new.py"]
    placed = pytest_shard.assign(moved, 4, pytest_shard.load_weights())
    assert partition_problems(placed, moved, 4) == []
    assert "ansible/tests/repo/test_moved_somewhere_new.py" in placed


def test_a_dropped_file_is_flagged():
    """The reject half. Without it, a checker that always returned no problems would pass."""
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, 4, pytest_shard.load_weights())
    del placed[files[0]]
    assert partition_problems(placed, files, 4) == [
        f"assigned to no shard: {[files[0]]}"
    ]


def test_an_out_of_range_shard_is_flagged():
    """The second reject half: a file placed in a shard the matrix does not run."""
    files = pytest_shard.census()
    placed = pytest_shard.assign(files, 4, pytest_shard.load_weights())
    placed[files[0]] = 9
    assert any(
        "outside 0..3" in problem for problem in partition_problems(placed, files, 4)
    )


def test_an_empty_shard_is_flagged():
    """The third: more shards than files leaves a leg collecting nothing, which pytest exits 5
    on rather than passing."""
    files = pytest_shard.census()[:2]
    placed = pytest_shard.assign(files, 4, pytest_shard.load_weights())
    assert any(
        "is empty" in problem for problem in partition_problems(placed, files, 4)
    )


def test_the_recorded_weights_still_cover_most_of_the_suite():
    files = pytest_shard.census()
    unweighted = [f for f in files if f not in pytest_shard.load_weights()]
    assert len(unweighted) <= MAX_UNWEIGHTED_FRACTION * len(files), (
        f"{len(unweighted)} of {len(files)} test files have no recorded duration, so the shard "
        "balance is guesswork. Re-record: uv run python scripts/dev/pytest_shard.py --record"
    )


def _pytest_job() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["pytest"]


def test_the_matrix_is_a_contiguous_one_based_range():
    """`--shard` is 1-based and `shard_files` rejects anything outside 1..N, so a matrix list
    that skips or starts at 0 fails the leg rather than mis-splitting — but it fails it on every
    run, which is a worse way to find out than here."""
    shards = _pytest_job()["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1)), (
        f"ci.yml matrix shard list is {shards}"
    )


def test_the_workflow_derives_the_shard_count_from_the_matrix():
    """The drift this guard exists for. A literal `--of 4` beside a matrix of any other length
    silently drops or double-runs part of the suite, and every leg still reports green."""
    steps = _pytest_job()["steps"]
    selects = [s for s in steps if "pytest_shard.py" in str(s.get("run", ""))]
    assert len(selects) == 1, "expected exactly one step to invoke pytest_shard.py"
    step = selects[0]
    assert re.search(r'--of\s+"\$SHARDS"', step["run"]), (
        "the shard-selection step must pass --of from the environment, not a literal count"
    )
    assert step["env"]["SHARDS"] == "${{ strategy.job-total }}", (
        f"SHARDS is {step['env']['SHARDS']!r}, not the matrix's own size"
    )
