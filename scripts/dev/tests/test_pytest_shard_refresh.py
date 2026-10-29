"""`pytest_shard.py --refresh-drifted`: the cron's repair for a weight that drifted moderately.

The census and the measurement are passed in, so these run no pytest subprocess and read no
committed table. The real measurement is `measure_weights`, which `--record-files` already uses.
The runner-seconds table that `--record-runner` and `--refresh-runner` write and the split packs on
is tested last.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_refresh.py
"""

import json
import subprocess

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
    assert "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py" in pins
    assert sorted(set(pins) - set(pytest_shard.census())) == []
    # CI reads only the table, so a pin the table does not carry has no effect there.
    table = pytest_shard.load_weights()
    assert {p: table.get(p) for p in pins} == pins


# Two shard logs as `gh api .../jobs/<id>/logs` returns them: a timestamp on every line and
# pytest's colours. Module `a` runs in both, so it takes the median of 3.0 and 5.0.
_LOGS = [
    "2026-10-24T19:27:39.79Z \x1b[32m3.00s call     scripts/x/tests/test_a.py::t\x1b[0m\n"
    "2026-10-24T19:27:39.80Z 1.00s setup    scripts/x/tests/test_b.py::t\n",
    "2026-10-24T19:28:01.11Z 5.00s call     scripts/x/tests/test_a.py::t\n",
]


def test_runner_weights_take_each_modules_median_out_of_raw_job_logs():
    assert shard_weight_gate.runner_weights(_LOGS) == {
        "scripts/x/tests/test_a.py": 4.0,
        "scripts/x/tests/test_b.py": 1.0,
    }


def test_the_runner_table_keeps_only_the_census_and_refuses_an_empty_record(tmp_path):
    table = tmp_path / "runner.json"
    keep = ["scripts/x/tests/test_a.py"]
    assert shard_weight_gate.write_runner_weights(_LOGS, keep, table) == {keep[0]: 4.0}
    assert json.loads(table.read_text()) == {keep[0]: 4.0}
    with pytest.raises(ValueError, match="nothing to record"):
        shard_weight_gate.write_runner_weights(["no durations here"], keep, table)


def test_the_split_packs_runner_seconds_and_scales_the_unmeasured_rest():
    """The recorded weights pack the runner-heavy pair a, b into one shard of two; the runner
    seconds split them, and a module the runner table lacks scales by the median ratio."""
    a, b, c, d, e = FILES[:5]
    recorded = {a: 1.0, b: 1.0, c: 1.5, d: 0.5, e: 2.0}
    runner = {a: 9.0, b: 9.0, c: 1.0, d: 1.0}
    assert (
        len(set(pytest_shard.assign([a, b, c, d], 2, recorded)[f] for f in (a, b))) == 1
    )
    weights = shard_weight_gate.packing_weights(recorded, runner)
    # The ratios are 9, 9, 0.67 and 2, so their median is 5.5.
    assert weights == runner | {e: 11.0}
    assert (
        len(set(pytest_shard.assign([a, b, c, d], 2, weights)[f] for f in (a, b))) == 2
    )


def test_the_committed_runner_table_is_populated():
    """Non-vacuity: an empty or unreadable table would pack every module on scaled weights.

    No named module and no value bound. The docs-refresh cron rewrites this table and runs
    the suite before it commits, so a test an honest refresh can break would park the cron
    on its own output. No census check either: an entry for a deleted module is ignored, so
    a PR deleting a test module need not touch the table (#5989 is the same trap on the
    weight table)."""
    assert len(shard_weight_gate.load_runner_weights()) >= 100


def test_a_runner_refresh_adds_rewrites_drifted_keeps_unmeasured_and_drops_deleted(
    tmp_path,
):
    a, b, c, d, e = FILES[:5]
    table = tmp_path / "runner.json"
    # a drifts 2x, b moves inside the band, c is not in these logs, d left the census.
    table.write_text(json.dumps({a: 2.0, b: 10.0, c: 3.0, d: 1.0}))
    logs = [f"8.00s call {a}::t\n12.00s call {b}::t\n1.00s call {e}::t\n"]
    moved = pytest_shard.refresh_runner_weights(logs, [a, b, c, e], table)
    assert moved == [a, e]
    assert json.loads(table.read_text()) == {a: 8.0, b: 10.0, c: 3.0, e: 1.0}


def test_a_runner_refresh_with_nothing_drifted_writes_the_same_bytes(tmp_path):
    """The cron publishes on any staged diff, so a quiet run must leave the file as it was."""
    a = FILES[0]
    table = tmp_path / "runner.json"
    shard_weight_gate.write_runner_weights([f"4.00s call {a}::t\n"], [a], table)
    before = table.read_bytes()
    assert (
        pytest_shard.refresh_runner_weights([f"4.40s call {a}::t\n"], [a], table) == []
    )
    assert table.read_bytes() == before


def _fake_gh(runs: dict[int, list[tuple[str, str]]]):
    """A `lib.gh.gh` stand-in: `runs` maps a run id to its jobs' (name, conclusion)."""
    calls = []

    def gh(*args, cwd=None):
        calls.append(args)
        if args[:2] == ("run", "list"):
            out = json.dumps([{"databaseId": run} for run in runs])
        elif args[:2] == ("run", "view"):
            jobs = runs[int(args[2])]
            out = json.dumps(
                {
                    "jobs": [
                        {
                            "databaseId": int(args[2]) * 10 + i,
                            "name": n,
                            "conclusion": c,
                        }
                        for i, (n, c) in enumerate(jobs)
                    ]
                }
            )
        else:
            out = f"log of {args[-1]}"
        return subprocess.CompletedProcess(args, 0, out, "")

    return gh, calls


def test_the_runner_fetch_reads_green_pytest_legs_of_the_newest_runs_that_ran_them():
    """Run 6 skipped its matrix, as a push outside its paths does; run 9 is past the two."""
    gh, calls = _fake_gh(
        {
            7: [("pytest (shard 1 of 2)", "success"), ("prek", "success")],
            6: [("pytest (shard ${{ matrix.shard }} of 2)", "skipped")],
            8: [
                ("pytest (shard 2 of 2)", "success"),
                ("pytest (shard 1 of 2)", "skipped"),
            ],
            9: [("pytest (shard 1 of 2)", "success")],
        }
    )
    assert pytest_shard.fetch_runner_logs(2, gh) == [
        "log of repos/{owner}/{repo}/actions/jobs/70/logs",
        "log of repos/{owner}/{repo}/actions/jobs/80/logs",
    ]
    listing = " ".join(calls[0])
    for wanted in (
        "--workflow ci.yml",
        "--branch master",
        "--event push",
        "--status success",
    ):
        assert wanted in listing


def test_the_runner_fetch_refuses_fewer_runs_than_asked():
    """One run is one noisy reading per module; the merge must never see it alone."""
    gh, _ = _fake_gh(
        {7: [("pytest (shard 1 of 2)", "success")], 8: [("prek", "success")]}
    )
    with pytest.raises(RuntimeError, match="logs in 1 of the newest 2 green master"):
        pytest_shard.fetch_runner_logs(2, gh)


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
