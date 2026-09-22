"""The measured half of the shard-weight coverage gate (#2238).

`pytest_shard.durations_problems` is what CI's `--check-durations` step runs against the
durations report its own test step produced. The static half is the ratchet in
`ansible/tests/repo/test_pytest_shards_partition_the_suite.py`.

`test_the_parser_reads_a_report_pytest_just_wrote` drives a real pytest subprocess on purpose:
every other test here feeds the parser a frozen string, so a change to pytest's report format
would leave them all green over a parser that had stopped matching anything.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_durations.py
"""

import subprocess
import sys

import pytest_shard

# A report in the shape pytest prints: seconds, phase, nodeid. `setup` and `teardown` count
# alongside `call`, which is what makes an expensive module-scoped fixture visible.
REPORT = """============================== slowest durations ===============================
21.40s call     scripts/heavy/tests/test_new_module.py::test_the_long_one
4.10s setup    scripts/heavy/tests/test_new_module.py::test_the_long_one
0.90s call     scripts/heavy/tests/test_new_module.py::test_the_short_one
17.31s call     ansible/tests/k8s/test_secret_consumer_census.py::test_every_consumer
0.01s call     scripts/dev/tests/test_run_as_cron.py::test_a_shell_builtin_still_runs

(58 durations < 0.005s hidden.  Use -vv to show these durations.)
"""

NEW_MODULE = "scripts/heavy/tests/test_new_module.py"
RECORDED = {
    "ansible/tests/k8s/test_secret_consumer_census.py": 17.31,
    "scripts/dev/tests/test_run_as_cron.py": 0.05,
}


def test_the_parser_sums_every_phase_of_a_file():
    totals = pytest_shard.parse_durations(REPORT)
    assert totals[NEW_MODULE] == 26.4
    assert totals["ansible/tests/k8s/test_secret_consumer_census.py"] == 17.31
    assert len(totals) == 3


def test_a_heavy_unweighted_module_is_flagged():
    """The reject half, and the #2238 case itself: a heavy file in a directory whose recorded
    siblings say nothing about it, which the ratchet's neighbour arm cannot see."""
    problems = pytest_shard.durations_problems(REPORT, RECORDED)
    assert len(problems) == 1
    assert NEW_MODULE in problems[0] and "26.4s" in problems[0]


def test_a_heavy_module_that_is_already_recorded_is_clean():
    """The accept half. The heaviest file left in the report is 17.31s and the gate says
    nothing, because the split already packs it at that weight."""
    assert pytest_shard.durations_problems(REPORT, RECORDED | {NEW_MODULE: 26.4}) == []


def test_a_light_unweighted_module_is_clean():
    """Every new test file is unweighted on the run that introduces it, and almost every one
    of them is cheap. Only the cost decides."""
    weights = {
        NEW_MODULE: 26.4,
        "ansible/tests/k8s/test_secret_consumer_census.py": 17.31,
    }
    assert pytest_shard.durations_problems(REPORT, weights) == []


def test_the_threshold_decides():
    assert pytest_shard.durations_problems(REPORT, RECORDED, threshold=30.0) == []
    assert pytest_shard.durations_problems(REPORT, RECORDED, threshold=0.5) != []


def test_a_report_with_no_durations_is_itself_a_complaint():
    """Non-vacuity at run time. The gate finds its subject by parsing, so a format change or a
    test step that dropped `--durations=0` would otherwise leave it passing over nothing."""
    problems = pytest_shard.durations_problems("no durations here\n", RECORDED)
    assert len(problems) == 1 and "parsed no durations" in problems[0]


def test_the_parser_reads_a_report_pytest_just_wrote(tmp_path):
    """Non-vacuity against the installed pytest, not against a string this file also wrote.

    A frozen sample proves the regex matches a format someone typed out; only a real run
    proves it matches the format pytest emits today.
    """
    module = tmp_path / "test_measured_sample.py"
    module.write_text("import time\n\n\ndef test_sleeps():\n    time.sleep(0.05)\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n0",
            "--durations=0",
            "-p",
            "no:cacheprovider",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    totals = pytest_shard.parse_durations(proc.stdout)
    assert totals, f"parsed nothing out of pytest's own report:\n{proc.stdout[-2000:]}"
    # pytest writes nodeids relative to the rootdir it picked, which is tmp_path here.
    assert totals[module.name] >= 0.05


def _run_cli(tmp_path, report: str) -> int:
    """`--check-durations` over `report`, against the table this repo actually ships.

    No monkeypatch and no test-only flag: REPORT's heavy module is a path the census will
    never hold, so the committed table reads it as unweighted whatever else is in the table.
    """
    log = tmp_path / "durations.log"
    log.write_text(report)
    return pytest_shard.main(["--check-durations", str(log)])


def test_the_cli_exits_nonzero_on_a_heavy_unweighted_module(tmp_path, capsys):
    """The exit code is the whole contract with `ci.yml`: the step fails the leg or it does
    not."""
    assert _run_cli(tmp_path, REPORT) == 1
    printed = capsys.readouterr().out
    assert NEW_MODULE in printed and "--record-missing" in printed


def test_the_cli_exits_zero_when_every_heavy_module_is_recorded(tmp_path, capsys):
    """The same report with the unweighted module dropped. What is left is the recorded
    17.31s pole, which the split already packs at its measured cost."""
    recorded_only = "\n".join(
        line for line in REPORT.splitlines() if NEW_MODULE not in line
    )
    assert _run_cli(tmp_path, recorded_only + "\n") == 0
    assert "no unweighted module" in capsys.readouterr().out
