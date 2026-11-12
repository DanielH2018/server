"""The runner-seconds table the CI shards are packed on: `--record-runner`, `--refresh-runner`
and the `--leg-spread` reading of one run's legs.

The logs are inline strings and `gh` is a stand-in, so these read no committed table except
where a test names it, and make no network call.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_runner_table.py
"""

import json
import subprocess

import pytest
import pytest_shard
import shard_weight_gate

FILES = [f"scripts/x/tests/test_{name}.py" for name in "abcdefgh"]


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


def test_a_shard_with_no_fleet_render_consumer_is_charged_the_k8s_render_alone(
    tmp_path,
):
    (tmp_path / "test_plain.py").write_text("def test_x(): pass\n")
    assert pytest_shard.prebuild_seconds(["test_plain.py"], tmp_path) == (
        pytest_shard.K8S_RENDER_WALL_SECONDS * pytest_shard.CI_WORKERS
    )
    assert pytest_shard.prebuild_seconds([], tmp_path) == 0.0


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


def _leg_log(compile_seconds: float, readings: dict[str, float]) -> str:
    """A pytest leg's log: the cold `uv sync` compile of 2000 files, then its durations."""
    lines = [
        f"2026-11-08T22:53:19.4Z Bytecode compiled 2000 files in {compile_seconds}s",
        "2026-11-08T22:53:20.1Z Bytecode compiled 2000 files in 60ms",
    ]
    lines += [
        f"2026-11-08T22:56:30.1Z {s:.2f}s call     {m}::t" for m, s in readings.items()
    ]
    return "\n".join(lines) + "\n"


def test_a_runner_refresh_reads_a_slow_and_a_fast_leg_as_the_same_modules(tmp_path):
    """The same modules on a leg twice as slow as two others (#7708 measured 0.66x to 1.22x).

    Read as measured, the fast legs' median sits 2x under what the slow leg recorded and both
    rows drift. Each leg's compile ran at its leg's speed, so every reading scales the same.
    """
    a, b = FILES[:2]
    table = tmp_path / "runner.json"
    slow = _leg_log(2.4, {a: 12.0, b: 24.0})
    fast = _leg_log(1.2, {a: 6.0, b: 12.0})
    recorded = shard_weight_gate.write_runner_weights([slow], [a, b], table)

    assert pytest_shard.refresh_runner_weights([fast, fast, slow], [a, b], table) == []
    assert json.loads(table.read_text()) == recorded


def test_a_leg_whose_only_compile_was_warm_is_read_as_measured():
    """A warm compile found its bytecode built, so it measures no CPU speed."""
    a = FILES[0]
    log = f"Bytecode compiled 2000 files in 60ms\n4.00s call {a}::t\n"
    assert shard_weight_gate.runner_weights([log]) == {a: 4.0}


def _summary(wall: float) -> str:
    return f"2026-11-09T00:41:10.0Z \x1b[32m==== \x1b[1m12 passed\x1b[0m in {wall}s (0:03:20) ====\n"


def test_the_leg_spread_takes_the_hardware_out_of_each_legs_wall():
    """Twice the compile seconds and twice the wall is the same split on slower hardware."""
    a = FILES[0]
    spread = shard_weight_gate.leg_spread(
        {
            "pytest (shard 2 of 3)": _leg_log(2.4, {a: 1.0}) + _summary(200.0),
            "pytest (shard 1 of 3)": _leg_log(1.2, {a: 1.0}) + _summary(100.0),
            "pytest (shard 3 of 3)": _leg_log(1.2, {a: 1.0}) + _summary(120.0),
        }
    )
    assert spread.splitlines() == [
        "3 pytest legs: normalised gap 28.3s, raw gap 100.0s",
        "pytest (shard 1 of 3): 100.00s wall, leg_speed 0.706, 141.67s normalised",
        "pytest (shard 2 of 3): 200.00s wall, leg_speed 1.412, 141.67s normalised",
        "pytest (shard 3 of 3): 120.00s wall, leg_speed 0.706, 170.00s normalised",
    ]


def test_the_leg_spread_refuses_a_leg_with_no_pytest_summary():
    """A leg cut off before its summary has no wall; reading 0s would invent the fastest leg."""
    with pytest.raises(ValueError, match=r"pytest \(shard 1 of 2\): no pytest summary"):
        shard_weight_gate.leg_spread({"pytest (shard 1 of 2)": _leg_log(1.2, {})})
    with pytest.raises(ValueError, match="no green pytest leg"):
        shard_weight_gate.leg_spread({})


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
    assert shard_weight_gate.fetch_runner_logs(2, gh) == [
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
        shard_weight_gate.fetch_runner_logs(2, gh)


def test_a_shard_holding_a_census_file_is_charged_the_census_pass(tmp_path):
    (tmp_path / "test_plain.py").write_text("def test_x(): pass\n")
    census = "scripts/tests/test_census_rows_test_renders.py"
    (tmp_path / census).parent.mkdir(parents=True)
    (tmp_path / census).write_text("def test_x(): pass\n")
    k8s = pytest_shard.K8S_RENDER_WALL_SECONDS
    assert pytest_shard.prebuild_seconds(["test_plain.py", census], tmp_path) == (
        (k8s + pytest_shard.CENSUS_PASS_WALL_SECONDS) * pytest_shard.CI_WORKERS
    )
