"""`pytest_shard.py --refresh-drifted`: the cron's repair for a weight that drifted moderately.

The census and the measurement are passed in, so these run no pytest subprocess and read no
committed table. The real measurement is `measure_weights`, which `--record-files` already uses.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_refresh.py
"""

import json

import pytest
import pytest_shard

FILES = [f"scripts/x/tests/test_{name}.py" for name in "abcdefgh"]


@pytest.mark.parametrize(
    ("recorded", "measured", "expected"),
    [
        pytest.param(7.31, 12.16, True, id="grew-past-the-band"),
        pytest.param(12.0, 4.0, True, id="shrank-past-the-band"),
        pytest.param(7.31, 10.5, False, id="inside-the-band"),
        pytest.param(0.02, 0.05, False, id="under-the-floor"),
        pytest.param(0.0, 0.8, True, id="free-entry-past-the-floor"),
    ],
)
def test_drift_is_a_ratio_past_a_floor(recorded, measured, expected):
    assert pytest_shard.drifted(recorded, measured) is expected


def test_a_refresh_rewrites_only_the_drifted_entries_of_its_own_slice(tmp_path):
    slices = 2
    ours = [f for f in FILES if pytest_shard.refresh_slice(f, slices) == 0]
    theirs = [f for f in FILES if f not in ours]
    assert len(ours) >= 2 and theirs, "the fixture names need files in both slices"
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({f: 2.0 for f in FILES}))
    # Every file measures far past its weight except the first of this slice, which is noise.
    measured = {f: 6.0 for f in FILES} | {ours[0]: 2.5}
    asked = []

    def measure(files):
        asked.extend(files)
        return {f: measured[f] for f in files}

    moved = pytest_shard.refresh_drifted_weights(
        0, slices, table, files=FILES, measure=measure
    )

    written = json.loads(table.read_text())
    assert sorted(asked) == sorted(ours)
    assert moved == sorted(ours[1:])
    assert {f: written[f] for f in ours} == {ours[0]: 2.0} | {f: 6.0 for f in ours[1:]}
    assert all(written[f] == 2.0 for f in theirs)


def test_successive_cron_runs_visit_every_slice():
    """Twelve-hour steps, the cron's own spacing, cover all eight slices in eight runs."""
    start = 1_791_900_000  # An arbitrary second in October 2026.
    visited = [pytest_shard.slice_at(start + run * 43_200, 8) for run in range(8)]
    assert sorted(visited) == list(range(8))
