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

import math
import re
from pathlib import PurePosixPath

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
# correctness — re-record with `uv run python scripts/dev/pytest_shard.py --record-missing`.
MAX_UNWEIGHTED_FRACTION = 0.2

# The count above cannot see the case that matters: cost is concentrated, so one unweighted
# 30s module is one file to the count and 30s to the shard it lands in (#2225, the shards at
# 65/99/99/68s against a 43s projection). Its weight is unknowable without running it, but its
# NEIGHBOURS' weights are on record, and the modules that cost seconds cluster in the
# directories whose tests drive a subprocess: the 2026-09-17 module landed beside a recorded
# 27.75s sibling. So an unweighted file in a directory that already holds one of the heaviest
# 1% of recorded modules fails on its own, however few such files there are. A share of the
# table rather than a number of seconds, so the bar moves with the suite: the heaviest 1%
# starts near 4s, where a decile started at 0.29s and named nearly every directory. A share by
# COUNT rather than a value quantile, because a table whose 99th percentile ties at the
# common value would call every directory a pole.
POLE_SHARE = 0.01

RECORD_MISSING = "uv run python scripts/dev/pytest_shard.py --record-missing"


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


def pole_directories(weights: dict[str, float], share: float = POLE_SHARE) -> set[str]:
    """The directories holding one of the heaviest `share` of recorded modules, at least one."""
    heaviest = sorted(weights, key=lambda f: (-weights[f], f))
    poles = heaviest[: math.ceil(share * len(heaviest))]
    return {str(PurePosixPath(f).parent) for f in poles}


def coverage_problems(files, weights: dict[str, float]) -> list[str]:
    """What is wrong with how well the table covers `files`, as readable complaints.

    Two arms. The count arm is the original ratchet: too many unweighted files and the split
    is guesswork everywhere. The neighbour arm is the one that sees a single heavy module: an
    unweighted file beside a recorded pole is assumed to cost what its neighbours cost until
    it is measured. Weights for files outside the census are ignored, so a deleted module
    does not keep its directory a pole.
    """
    problems = []
    unweighted = [f for f in files if f not in weights]
    if len(unweighted) > MAX_UNWEIGHTED_FRACTION * len(files):
        problems.append(
            f"{len(unweighted)} of {len(files)} test files have no recorded duration, so "
            "the shard balance is guesswork"
        )
    recorded = {f: weights[f] for f in files if f in weights}
    poles = pole_directories(recorded)
    if beside := [f for f in unweighted if str(PurePosixPath(f).parent) in poles]:
        problems.append(
            f"unweighted beside a recorded pole, so packed as the lightest thing in the "
            f"suite when its neighbours say otherwise: {beside}"
        )
    return problems


def test_the_recorded_weights_still_cover_most_of_the_suite():
    """The ratchet. Its node id is spelled in `pytest_shard.RATCHET_NODE_ID` and deselected by
    the two crons that commit with hooks on, so it stays ONE test: a second one with the same
    repair would fail those commits the way #1899 did."""
    files = pytest_shard.census()
    problems = coverage_problems(files, pytest_shard.load_weights())
    assert problems == [], "\n".join([*problems, f"Repair (seconds): {RECORD_MISSING}"])


def test_too_many_unweighted_files_are_flagged():
    """Reject half of the count arm."""
    files = pytest_shard.census()
    weights = {f: 0.1 for f in files[: len(files) // 2]}
    assert any("guesswork" in p for p in coverage_problems(files, weights))


def test_a_light_unweighted_file_in_a_quiet_directory_is_clean():
    """Accept half of the neighbour arm: a new file where nothing recorded is heavy."""
    weights = {f"ansible/tests/repo/test_{i}.py": 0.1 for i in range(99)}
    weights["scripts/deploy_tools/tests/test_pole.py"] = 27.75
    files = [*weights, "ansible/tests/repo/test_new.py"]
    assert coverage_problems(files, weights) == []


def test_an_unweighted_file_beside_a_recorded_pole_is_flagged():
    """Reject half, and the 2026-09-17 input itself: `test_gitops_tick_wrapper.py` landed
    unrecorded beside `test_deploy_service_lock_concurrency.py` at 27.75s, and the count arm
    read one file as one file."""
    weights = {f"ansible/tests/repo/test_{i}.py": 0.1 for i in range(99)}
    weights["scripts/deploy_tools/tests/test_deploy_service_lock_concurrency.py"] = (
        27.75
    )
    landed = "scripts/deploy_tools/tests/test_gitops_tick_wrapper.py"
    problems = coverage_problems([*weights, landed], weights)
    assert len(problems) == 1 and landed in problems[0], problems


def test_a_deleted_pole_no_longer_marks_its_directory():
    """A weight for a file the census no longer holds must not keep the neighbour arm armed."""
    weights = {f"ansible/tests/repo/test_{i}.py": 0.1 for i in range(99)}
    weights["scripts/deploy_tools/tests/test_deleted_pole.py"] = 27.75
    files = [f for f in weights if "deleted" not in f]
    files.append("scripts/deploy_tools/tests/test_new.py")
    assert coverage_problems(files, weights) == []


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
