"""`pytest_shard.py --refresh-drifted`: the cron's repair for a weight that drifted moderately.

The census and the measurement are passed in, so these run no pytest subprocess and read no
committed table. The real measurement is `measure_weights`, which `--record-files` already uses.
The runner-seconds table has its own module, `test_pytest_shard_runner_table.py`.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_refresh.py
"""

import json

import pytest
import pytest_shard
import shard_weight_gate

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
        asked.append(list(files))
        return {f: measured[f] for f in files}

    moved = pytest_shard.refresh_drifted_weights(
        0, slices, table, files=FILES, measure=measure
    )

    written = json.loads(table.read_text())
    assert sorted(asked[0]) == sorted(ours)
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
    assert "k8s/roles/crowdsec/tests/test_jobs_image.py" in pins
    assert sorted(set(pins) - set(pytest_shard.census())) == []
    # CI reads only the table, so a pin the table does not carry has no effect there.
    table = pytest_shard.load_weights()
    assert {p: table.get(p) for p in pins} == pins


def _runs(*results):
    """A `measure` whose successive calls return `results` in order, the last one repeating.

    Each result maps a path to its seconds; a path a result omits measured nothing, which the
    refresh reads as 0.0. `calls` keeps the files each call was asked for.
    """
    calls = []

    def measure(files):
        calls.append(list(files))
        result = results[min(len(calls), len(results)) - 1]
        return {f: result[f] for f in files if f in result}

    return measure, calls


@pytest.mark.parametrize(
    ("recorded", "noisy", "settled"),
    [
        # docs-refresh/2026-10-19-0624: one run measured 0.0s, CI measured 15.8s.
        pytest.param(16.0, None, 15.8, id="one-run-measured-nothing"),
        # docs-refresh/2026-10-23-0624: one run measured 62.36s, CI measured 1.03s.
        pytest.param(0.57, 62.36, 1.03, id="one-run-measured-60x"),
    ],
)
def test_a_refresh_keeps_a_row_one_outlying_measurement_would_rewrite(
    tmp_path, recorded, noisy, settled
):
    """#6286: a re-measurement that disagrees with the drifted one leaves the row alone."""
    module = FILES[0]
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({module: recorded}))
    first = {} if noisy is None else {module: noisy}
    measure, _ = _runs(first, {module: settled})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    assert moved == []
    assert json.loads(table.read_text()) == {module: recorded}


def test_a_refresh_re_measures_only_the_files_that_drifted(tmp_path):
    """#6286: the confirming run costs only the drifted files, not the slice again."""
    noisy, *steady = FILES
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({f: 2.0 for f in FILES}))
    measure, calls = _runs({f: 2.0 for f in steady} | {noisy: 9.0})

    pytest_shard.refresh_drifted_weights(
        0, 1, table, files=FILES, measure=measure, pins={}
    )

    assert sorted(calls[0]) == sorted(FILES)
    re_measured = [f for call in calls[1:] for f in call]
    assert set(re_measured) == {noisy}


def test_a_refresh_keeps_a_row_whose_two_runs_drift_but_disagree(tmp_path):
    """#6286: both runs leaving the band is not enough; they must agree with each other."""
    module = FILES[0]
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({module: 16.0}))
    measure, _ = _runs({module: 0.0}, {module: 40.0})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    assert moved == []
    assert json.loads(table.read_text()) == {module: 16.0}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param({}, {}, id="both-runs-omit-it"),
        pytest.param({}, {FILES[0]: 0.3}, id="only-the-first-run-omits-it"),
    ],
)
def test_a_refresh_reads_a_file_a_run_omitted_as_0_0(tmp_path, first, second):
    """A file whose tests are all deselected is absent from a run and costs its import (#6587)."""
    module = FILES[0]
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({module: 16.0}))
    measure, _ = _runs(first, second)

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    assert moved == [module]
    assert json.loads(table.read_text()) == {module: 0.0}


def test_a_refresh_writes_the_lower_of_two_agreeing_runs(tmp_path):
    """#6587: two loaded runs can agree with each other and still both overshoot."""
    module = FILES[0]
    table = tmp_path / "weights.json"
    table.write_text(json.dumps({module: 0.8}))
    measure, _ = _runs({module: 20.0}, {module: 23.39})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    assert moved == [module]
    assert json.loads(table.read_text()) == {module: 20.0}


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        # docs-refresh/2026-10-29-1828: one run recorded 23.39s, CI measured 0.91s (#6587).
        pytest.param(0.8, 0.8, id="a-loaded-first-run"),
        pytest.param(24.0, 23.39, id="a-loaded-second-run"),
        pytest.param(None, 23.39, id="a-second-run-that-measured-nothing"),
    ],
)
def test_a_heavy_new_row_keeps_the_lower_of_two_readings(tmp_path, second, expected):
    module = FILES[0]
    table = tmp_path / "weights.json"
    table.write_text("{}")
    measure, _ = _runs({module: 23.39}, {} if second is None else {module: second})

    pytest_shard.record_missing_weights(table, files=[module], measure=measure)

    assert json.loads(table.read_text()) == {module: expected}


def _runner_rows(count, *, heaviest=False):
    """`count` rows of the committed runner table as (module, seconds), from one end of it.

    The refusal reads that table by default, as the cron's call does. The cron rewrites it, so
    every reading below derives from these rows, and a row is never named.
    """
    rows = [r for r in shard_weight_gate.load_runner_weights().items() if r[1] > 0]
    rows.sort(key=lambda row: (row[1], row[0]), reverse=heaviest)
    assert len(rows) >= count, "the runner table has too few rows for this test"
    return rows[:count]


def _lightest_runner_rows(count):
    """The lightest rows, each light enough for every reading these tests write against it."""
    rows = _runner_rows(count)
    assert rows[-1][1] <= 0.1, "the runner table has no row this light"
    return rows


def _heaviest_runner_rows(count):
    """The heaviest rows, each far enough above the floor that only the ratio decides."""
    rows = _runner_rows(count, heaviest=True)
    floor = shard_weight_gate.STALE_WEIGHT_FLOOR_SECONDS
    ratio = shard_weight_gate.LOCAL_READING_MAX_RATIO
    assert rows[-1][1] * ratio * 0.9 >= floor, "the runner table has no row this heavy"
    return rows


def _table(tmp_path, rows):
    table = tmp_path / "weights.json"
    table.write_text(json.dumps(rows))
    return table


def test_a_refresh_keeps_a_row_the_runner_table_says_ci_would_reject(tmp_path):
    """#7132: two agreeing runs on the recording host do not outvote CI's own seconds."""
    [(module, _)] = _lightest_runner_rows(1)
    table = _table(tmp_path, {module: 2.0})
    measure, _ = _runs({module: 85.0})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    assert moved == []
    assert json.loads(table.read_text()) == {module: 2.0}


def test_a_refresh_still_writes_a_row_the_runner_table_lacks(tmp_path):
    """#7132: only a module CI has measured can be refused, so a new one's 85s still lands."""
    [(held, _)] = _lightest_runner_rows(1)
    unmeasured = FILES[0]
    assert unmeasured not in shard_weight_gate.load_runner_weights()
    table = _table(tmp_path, {held: 2.0, unmeasured: 2.0})
    measure, _ = _runs({held: 85.0, unmeasured: 85.0})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[held, unmeasured], measure=measure, pins={}
    )

    assert moved == [unmeasured]
    assert json.loads(table.read_text()) == {held: 2.0, unmeasured: 85.0}


def test_the_refusal_starts_at_the_gates_floor(tmp_path):
    """#7132: under the floor the gate compares nothing, so no ratio there can refuse a row."""
    floor = shard_weight_gate.STALE_WEIGHT_FLOOR_SECONDS
    (at_floor, _), (under_floor, _) = _lightest_runner_rows(2)
    table = _table(tmp_path, {at_floor: 1.0, under_floor: 1.0})
    measure, _ = _runs({at_floor: floor, under_floor: floor - 0.5})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[at_floor, under_floor], measure=measure, pins={}
    )

    assert moved == [under_floor]
    assert json.loads(table.read_text()) == {at_floor: 1.0, under_floor: floor - 0.5}


def test_the_refusal_starts_at_the_local_reading_ratio(tmp_path):
    """#7132: a reading just under `LOCAL_READING_MAX_RATIO` is written, and one just over is not."""
    ratio = shard_weight_gate.LOCAL_READING_MAX_RATIO
    (over, over_runner), (under, under_runner) = _heaviest_runner_rows(2)
    under_reading = under_runner * ratio * 0.9
    table = _table(tmp_path, {over: 1.0, under: 1.0})
    measure, _ = _runs({over: over_runner * ratio * 1.1, under: under_reading})

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[over, under], measure=measure, pins={}
    )

    assert moved == [under]
    assert json.loads(table.read_text()) == {over: 1.0, under: under_reading}


def test_the_refusal_judges_the_lower_reading_the_row_would_hold(tmp_path):
    """#7132: the row holds the lower of the two runs, so that reading is what the gate sees."""
    (refused, refused_runner), (written, written_runner) = _heaviest_runner_rows(2)
    table = _table(tmp_path, {refused: 1.0, written: 1.0})
    measure, _ = _runs(
        {refused: refused_runner * 28, written: written_runner * 28},
        {refused: refused_runner * 25, written: written_runner * 19},
    )

    moved = pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[refused, written], measure=measure, pins={}
    )

    assert moved == [written]
    assert json.loads(table.read_text()) == {
        refused: 1.0,
        written: written_runner * 19,
    }


def test_a_refresh_prints_the_module_it_refused(tmp_path, capsys):
    """#7132: a refused row leaves no diff, so the printed report is the only trace of it."""
    [(module, _)] = _lightest_runner_rows(1)
    table = _table(tmp_path, {module: 2.0})
    measure, _ = _runs({module: 85.0})

    pytest_shard.refresh_drifted_weights(
        0, 1, table, files=[module], measure=measure, pins={}
    )

    captured = capsys.readouterr()
    assert module in captured.out + captured.err


def test_a_host_only_new_row_holds_the_runner_seconds(tmp_path, capsys):
    """#7136: an 85s row is a cost of this host alone, and leaving the row out re-measures it
    on every tick."""
    [(module, runner)] = _lightest_runner_rows(1)
    table = _table(tmp_path, {})
    measure, calls = _runs({module: 85.0}, {module: 85.0})

    pytest_shard.record_missing_weights(table, files=[module], measure=measure)

    written = json.loads(table.read_text())
    assert written == {module: runner}
    assert module in capsys.readouterr().out
    report = f"{runner}s call {module}::t\n"
    assert shard_weight_gate.durations_complaints(report, written) == []

    measured_so_far = len(calls)
    pytest_shard.record_missing_weights(table, files=[module], measure=measure)
    assert len(calls) == measured_so_far


def test_a_light_new_row_is_measured_once(tmp_path):
    """Under the gate's floor no ratio arm compares the row, so a second run buys nothing."""
    light, heavy = FILES[:2]
    table = tmp_path / "weights.json"
    table.write_text("{}")
    measure, calls = _runs({light: 0.4, heavy: 9.0}, {heavy: 3.1})

    pytest_shard.record_missing_weights(table, files=[light, heavy], measure=measure)

    assert json.loads(table.read_text()) == {light: 0.4, heavy: 3.1}
    assert calls[1:] == [[heavy]]
