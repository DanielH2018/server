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


PINNED = FILES[0]
PINS = {PINNED: 16.0}


def _free(files=None):
    """A local measurement, where the pinned module's legs skip and every file costs 0s."""
    return {}


@pytest.fixture
def pinned_table(tmp_path):
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({f: 16.0 if f == PINNED else 0.0 for f in FILES}))
    return table


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(pytest_shard.record_weights, id="record"),
        pytest.param(
            lambda table, **kw: pytest_shard.record_named_weights(
                [PINNED], table, **kw
            ),
            id="record-files",
        ),
    ],
)
def test_a_record_keeps_a_ci_only_pin_its_local_run_measures_as_free(
    pinned_table, record
):
    record(pinned_table, files=FILES, measure=_free, pins=PINS)
    assert json.loads(pinned_table.read_text())[PINNED] == 16.0


def test_a_refresh_neither_measures_nor_moves_a_ci_only_pin(pinned_table):
    asked = []
    for index in range(2):
        moved = pytest_shard.refresh_drifted_weights(
            index,
            2,
            pinned_table,
            files=FILES,
            measure=lambda f: asked.extend(f) or {},
            pins=PINS,
        )
        assert PINNED not in moved
    assert PINNED not in asked
    assert json.loads(pinned_table.read_text())[PINNED] == 16.0


def test_every_ci_only_pin_names_a_module_the_shards_run():
    pins = pytest_shard.CI_ONLY_WEIGHTS
    assert "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py" in pins
    assert sorted(set(pins) - set(pytest_shard.census())) == []
    # CI reads only the table, so a pin the table does not carry has no effect there.
    table = pytest_shard.load_weights()
    assert {p: table.get(p) for p in pins} == pins
