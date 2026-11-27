"""The pytest shards partition the files CI selects, over generated trees and weights.

A file `assign` drops from every leg never runs, and CI still reports green. A tree is up to
five directories of test files plus up to three census files, with runner weights for a random
subset, so a whole directory often has no row and packs at the median. No path exists in the
repo or in the empty directory `assign` reads, so no unit triggers a prebuild. The property
tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/dev/tests/ci/test_pytest_shard_properties.py
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dev import pytest_shard
from dev.shard_prebuild import CENSUS_FILE_PREFIX

# Float sums over a few dozen costs reorder the last bits; a real gap is whole seconds.
_FLOAT_SLACK = 1e-6

_PLAIN = st.builds("gen{}/test_{}.py".format, st.integers(0, 4), st.integers(0, 15))
# Repeated values make tied costs common, which the path tie-break must settle.
_SECONDS = st.sampled_from([0.0, 2.5, 40.0]) | st.floats(0, 300)
_CENSUS = st.builds(f"{CENSUS_FILE_PREFIX}gen{{}}.py".format, st.integers(0, 9))


@st.composite
def _trees(draw) -> tuple[list[str], dict[str, float]]:
    files = draw(st.lists(_PLAIN, min_size=1, max_size=40, unique=True))
    files += draw(st.lists(_CENSUS, max_size=3, unique=True))
    weighted = draw(st.lists(st.sampled_from(files), unique=True))
    weights = {f: draw(_SECONDS) for f in weighted}
    return draw(st.permutations(files)), weights


_SHARDS = st.integers(1, 12)


@pytest.fixture(scope="module")
def empty_repo(tmp_path_factory):
    return tmp_path_factory.mktemp("repo")


@given(_trees(), _SHARDS, st.data())
def test_every_file_lands_in_exactly_one_leg_and_the_census_files_share_one(
    tree, shards, data
):
    files, weights = tree
    repeated = files + data.draw(st.lists(st.sampled_from(files), max_size=5))
    legs = [
        pytest_shard.shard_files(k, shards, repeated, weights)
        for k in range(1, shards + 1)
    ]

    placed = [f for leg in legs for f in leg]
    assert sorted(placed) == sorted(set(files))
    census_legs = {
        i for i, leg in enumerate(legs) for f in leg if f.startswith(CENSUS_FILE_PREFIX)
    }
    assert len(census_legs) == any(f.startswith(CENSUS_FILE_PREFIX) for f in files)


@given(_trees(), _SHARDS, st.data())
def test_the_assignment_ignores_the_order_of_the_input(empty_repo, tree, shards, data):
    files, weights = tree
    reordered = data.draw(st.permutations(files))
    expected = pytest_shard.assign(files, shards, weights, empty_repo)

    assert pytest_shard.assign(reordered, shards, weights, empty_repo) == expected
    assert (
        pytest_shard.assign(list(reversed(files)), shards, weights, empty_repo)
        == expected
    )


@given(_trees(), _SHARDS)
def test_with_no_prebuilds_the_legs_differ_by_at_most_the_largest_unit(
    empty_repo, tree, shards
):
    files, weights = tree
    placed = pytest_shard.assign(files, shards, weights, empty_repo)
    cost = pytest_shard._costs(files, weights)
    census = [f for f in files if f.startswith(CENSUS_FILE_PREFIX)]
    units = [cost[f] for f in files if f not in census] + [sum(cost[f] for f in census)]
    loads = [0.0] * shards
    for f, leg in placed.items():
        loads[leg] += cost[f]

    assert max(loads) - min(loads) <= max(units) + _FLOAT_SLACK


@given(_trees(), _SHARDS, st.data())
def test_the_leg_count_stays_in_range_grows_with_the_selection_and_fills_every_leg_for_all(
    tree, shards, data
):
    files, weights = tree
    order = data.draw(st.permutations(files))
    counts = [
        pytest_shard.legs_for(order[:size], files, shards, weights)
        for size in range(1, len(order) + 1)
    ]

    assert all(1 <= n <= shards for n in counts)
    assert counts == sorted(counts)
    assert counts[-1] == shards
