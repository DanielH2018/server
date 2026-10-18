"""The measured half of the shard-weight coverage gate.

`shard_weight_gate.durations_problems` is what CI's `--check-durations` step runs against the
durations report its own test step produced. The static half is the ratchet in
`ansible/tests/repo/test_pytest_shards_partition_the_suite.py`.

It has two arms over that one report. `heavy_unweighted` asks whether a costly file is MISSING
from the table; `stale_overweight` asks whether a recorded number is still roughly what
the file costs. Both are exercised through `durations_problems` with an explicit
weights dict rather than through the CLI: a synthetic path reads as unweighted against the
committed table, which is what lets the CLI tests below skip a monkeypatch, but no synthetic
path can be OVER-recorded in it.

`test_the_parser_reads_a_report_pytest_just_wrote` drives a real pytest subprocess on purpose:
every other test here feeds the parser a frozen string, so a change to pytest's report format
would leave them all green over a parser that had stopped matching anything.

Run: uv run pytest scripts/dev/tests/test_pytest_shard_durations.py
"""

import sys

import pytest
import pytest_shard
import shard_weight_gate
from lib.proc_testing import run

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
    totals = shard_weight_gate.parse_durations(REPORT)
    assert totals[NEW_MODULE] == 26.4
    assert totals["ansible/tests/k8s/test_secret_consumer_census.py"] == 17.31
    assert len(totals) == 3


def test_a_heavy_unweighted_module_is_flagged():
    """The reject half: a heavy file in a directory whose recorded
    siblings say nothing about it, which the ratchet's neighbour arm cannot see."""
    problems = shard_weight_gate.durations_problems(REPORT, RECORDED)
    assert len(problems) == 1
    assert NEW_MODULE in problems[0] and "26.4s" in problems[0]


def test_a_heavy_module_that_is_already_recorded_is_clean():
    """The accept half. The heaviest file left in the report is 17.31s and the gate says
    nothing, because the split already packs it at that weight."""
    assert (
        shard_weight_gate.durations_problems(REPORT, RECORDED | {NEW_MODULE: 26.4})
        == []
    )


def test_a_light_unweighted_module_is_clean():
    """Every new test file is unweighted on the run that introduces it, and almost every one
    of them is cheap. Only the cost decides."""
    weights = {
        NEW_MODULE: 26.4,
        "ansible/tests/k8s/test_secret_consumer_census.py": 17.31,
    }
    assert shard_weight_gate.durations_problems(REPORT, weights) == []


def test_the_threshold_decides():
    assert shard_weight_gate.durations_problems(REPORT, RECORDED, threshold=30.0) == []
    assert shard_weight_gate.durations_problems(REPORT, RECORDED, threshold=0.5) != []


# The unweighted module measuring 12.4s, as `test_fanout_red_gate.py` did on the PR run that
# failed and then 11.2s on its rerun (#4010): between the two bounds.
MARGIN_REPORT = (
    f"12.40s call     {NEW_MODULE}::test_the_long_one\n"
    "17.31s call     ansible/tests/k8s/test_secret_consumer_census.py::test_every_consumer\n"
)


def test_an_unweighted_module_between_the_bounds_annotates_rather_than_fails():
    assert shard_weight_gate.durations_problems(MARGIN_REPORT, RECORDED) == []
    warnings = shard_weight_gate.durations_warnings(MARGIN_REPORT, RECORDED)
    assert len(warnings) == 1
    assert NEW_MODULE in warnings[0] and "12.4s" in warnings[0]
    assert "--record-missing" in warnings[0]


def test_an_unweighted_module_past_the_fail_bound_fails_and_does_not_also_annotate():
    """REPORT's module measures 26.4s, past `RUNNER_HEAVY_FAIL_SECONDS`, so it is one failing
    complaint and no warning repeating it."""
    assert shard_weight_gate.durations_warnings(REPORT, RECORDED) == []
    assert shard_weight_gate.durations_problems(REPORT, RECORDED, fail_above=30.0) == []
    assert shard_weight_gate.durations_warnings(REPORT, RECORDED, fail_above=30.0) != []


def test_a_recorded_weight_far_above_what_it_measured_is_flagged():
    """A stale entry: the census module recorded at 17.31s, measuring 0.09s here, which the three
    missing-from-the-table arms cannot see."""
    stale = "17.31s call     ansible/tests/k8s/test_secret_consumer_census.py"
    report = REPORT.replace(stale, "0.09s call     " + stale.split()[-1])
    problems = shard_weight_gate.durations_problems(
        report, RECORDED | {NEW_MODULE: 26.4}
    )
    assert len(problems) == 1
    assert "17.31s" in problems[0] and "0.09s" in problems[0]
    assert "--record-files" in problems[0]


def test_a_recorded_weight_inside_the_runner_noise_is_clean():
    """The accept half, at the bound that decides it. Controls measured 1.10x to 1.46x their
    recorded values, so 17.31s recorded against 14.00s measured is the scale
    difference between the runner and the recording workstation, not a stale entry."""
    stale = "17.31s call     ansible/tests/k8s/test_secret_consumer_census.py"
    report = REPORT.replace(stale, "14.00s call     " + stale.split()[-1])
    assert (
        shard_weight_gate.durations_problems(report, RECORDED | {NEW_MODULE: 26.4})
        == []
    )


def test_the_floor_keeps_a_cheap_entry_out_of_it():
    """A file recorded at 0.05s measuring 0.01s is a 5x ratio and no shard notices. Only
    entries big enough to move the pole are compared."""
    report = (
        "0.01s call     scripts/dev/tests/test_run_as_cron.py::test_a_shell_builtin\n"
    )
    assert shard_weight_gate.stale_overweight(report, RECORDED) == []


def test_both_arms_report_together():
    """One report can carry a heavy unweighted module AND a stale entry, and a gate that
    short-circuited on the first would hide the second until the next run."""
    stale = "17.31s call     ansible/tests/k8s/test_secret_consumer_census.py"
    report = REPORT.replace(stale, "0.09s call     " + stale.split()[-1])
    problems = shard_weight_gate.durations_problems(report, RECORDED)
    assert len(problems) == 2
    assert NEW_MODULE in problems[0]
    assert "test_secret_consumer_census.py" in problems[1]


def test_the_stale_ratio_decides():
    stale = "17.31s call     ansible/tests/k8s/test_secret_consumer_census.py"
    report = REPORT.replace(stale, "0.09s call     " + stale.split()[-1])
    weights = RECORDED | {NEW_MODULE: 26.4}
    assert shard_weight_gate.durations_problems(report, weights, ratio=1000.0) == []
    assert shard_weight_gate.durations_problems(report, weights, ratio=2.0) != []


# `test_media_volume_verify.py` on #5598: recorded 4.86s on a host busy with fan-out batches,
# measured 0.97s on the runner (5.0x), and the PR that changed it went red (#5616).
BUSY_HOST_MODULE = "ansible/roles/k8s/media-volume/tests/test_media_volume_verify.py"
BUSY_HOST_REPORT = f"0.97s call     {BUSY_HOST_MODULE}::test_verify\n"


def test_an_entry_a_busy_host_over_recorded_annotates_rather_than_fails():
    """Under `STALE_WEIGHT_FAIL_RATIO`, a pull request that changed the module still passes
    (no problem, so `main` exits 0), and the entry stays visible as an annotation naming its
    repair."""
    weights = {BUSY_HOST_MODULE: 4.86}
    problems, warnings = shard_weight_gate.gate_complaints(
        BUSY_HOST_REPORT, weights, {BUSY_HOST_MODULE}
    )
    assert problems == [] and len(warnings) == 1
    assert "recorded 4.86s" in warnings[0] and "--record-files" in warnings[0]


def test_an_entry_recorded_twenty_times_what_it_measured_still_fails():
    """The fail half: the same changed module recorded at 20x its measurement."""
    weights = {BUSY_HOST_MODULE: 19.4}
    problems, warnings = shard_weight_gate.gate_complaints(
        BUSY_HOST_REPORT, weights, {BUSY_HOST_MODULE}
    )
    assert len(problems) == 1 and warnings == []
    assert "recorded 19.40s" in problems[0] and "measured 0.97s" in problems[0]


# REPORT's new module recorded at the 0.48s that `test_guard_tests_read_renders_not_templates.py`
# carried while it measured 45.67s on a master run (#4873): present in the table, and as good as
# missing from it.
UNDER_RECORDED = RECORDED | {NEW_MODULE: 0.48}


def test_a_heavy_module_recorded_far_below_what_it_measured_is_flagged():
    problems = shard_weight_gate.durations_problems(REPORT, UNDER_RECORDED)
    assert len(problems) == 1
    assert NEW_MODULE in problems[0]
    assert "recorded 0.48s" in problems[0] and "26.4s" in problems[0]
    assert "--record-files" in problems[0]


def test_a_heavy_module_inside_the_runner_scale_is_clean():
    """The accept half: 26.4s measured against 2.2s recorded is 12x, about the most a correctly
    recorded module has reached on the runner (`UNDER_WEIGHT_RATIO`). Only the ratio decides."""
    weights = RECORDED | {NEW_MODULE: 2.2}
    assert shard_weight_gate.durations_problems(REPORT, weights) == []
    assert shard_weight_gate.durations_problems(REPORT, weights, under_ratio=10.0) != []


# `test_karakeep_public_bypass.py` on that master run: recorded 0.0s, measured 7.41s, every
# second of it the k8s render charged to the first test on its worker.
CACHE_FIRST_REPORT = f"7.41s setup    {NEW_MODULE}::test_the_long_one\n"


def test_a_near_free_entry_measuring_over_five_seconds_annotates():
    """Below the unweighted arm's 10s, where four of #4873's six rows sat."""
    weights = RECORDED | {NEW_MODULE: 0.0}
    assert shard_weight_gate.durations_problems(CACHE_FIRST_REPORT, weights) == []
    warnings = shard_weight_gate.durations_warnings(CACHE_FIRST_REPORT, weights)
    assert len(warnings) == 1 and NEW_MODULE in warnings[0] and "7.4s" in warnings[0]


def test_an_under_recorded_module_under_five_seconds_is_clean():
    report = CACHE_FIRST_REPORT.replace("7.41s", "4.90s")
    weights = RECORDED | {NEW_MODULE: 0.0}
    assert shard_weight_gate.durations_warnings(report, weights) == []


def test_an_under_recorded_module_between_the_bounds_annotates_rather_than_fails():
    assert shard_weight_gate.durations_problems(MARGIN_REPORT, UNDER_RECORDED) == []
    warnings = shard_weight_gate.durations_warnings(MARGIN_REPORT, UNDER_RECORDED)
    assert len(warnings) == 1
    assert NEW_MODULE in warnings[0] and "12.4s" in warnings[0]
    assert "--record-files" in warnings[0]


def test_a_report_with_no_durations_is_itself_a_complaint():
    """Non-vacuity at run time. The gate finds its subject by parsing, so a format change or a
    test step that dropped `--durations=0` would otherwise leave it passing over nothing."""
    problems = shard_weight_gate.durations_problems("no durations here\n", RECORDED)
    assert len(problems) == 1 and "parsed no durations" in problems[0]


def test_the_parser_reads_a_report_pytest_just_wrote(tmp_path):
    """Non-vacuity against the installed pytest, not against a string this file also wrote.

    A frozen sample proves the regex matches a format someone typed out; only a real run
    proves it matches the format pytest emits today.
    """
    module = tmp_path / "test_measured_sample.py"
    module.write_text("import time\n\n\ndef test_sleeps():\n    time.sleep(0.05)\n")
    proc = run(
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
        cwd=tmp_path,
        # A nested pytest session pays its own collection and plugin load.
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    totals = shard_weight_gate.parse_durations(proc.stdout)
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


def _recorded_pole() -> str:
    """A durations line for the committed table's heaviest module, measuring what it records.

    The CLI reads the real table, so a report naming a real module at a made-up cost trips the
    over- or under-recording arm on whatever that module's entry holds today.
    """
    weights = pytest_shard.load_weights()
    path = max(weights, key=weights.__getitem__)
    return f"{weights[path]:.2f}s call     {path}::test_the_pole\n"


def test_the_cli_exits_zero_when_every_heavy_module_is_recorded(tmp_path, capsys):
    """A report whose only heavy module is the table's own pole, measuring its recorded cost,
    which the split already packs at that weight."""
    assert _run_cli(tmp_path, _recorded_pole()) == 0
    assert "no unweighted or under-recorded module" in capsys.readouterr().out


def test_the_cli_exits_zero_and_annotates_a_module_between_the_bounds(tmp_path, capsys):
    """The PR path: no `--warn-only`, and still exit 0, because the step is what failed 6
    branches in 10 runs on a module that measured either side of 10s (#4010)."""
    margin_only = MARGIN_REPORT.splitlines(keepends=True)[0]
    assert _run_cli(tmp_path, margin_only + _recorded_pole()) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("::warning::") and NEW_MODULE in printed


def test_warn_only_annotates_the_same_complaint_and_exits_zero(tmp_path, capsys):
    """CI passes `--warn-only` on a push to master, where nobody can apply the repair (#3604).
    The complaint must still reach the log as an annotation, not vanish with the exit code."""
    log = tmp_path / "durations.log"
    log.write_text(REPORT)
    assert pytest_shard.main(["--warn-only", "--check-durations", str(log)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("::warning::") and NEW_MODULE in printed


def test_a_pull_request_fails_on_a_bad_weight_its_diff_changes(tmp_path, capsys):
    """The fail half of the pair (#5480): the author touched the module, so the author can
    re-record it. With no `--changed-files` at all, as on a push, every module counts, which
    `test_the_cli_exits_nonzero_on_a_heavy_unweighted_module` covers."""
    changed = tmp_path / "changed.txt"
    changed.write_text(f"README.md\n{NEW_MODULE}\n")
    log = tmp_path / "durations.log"
    log.write_text(REPORT)
    argv = ["--check-durations", str(log), "--changed-files", str(changed)]
    assert pytest_shard.main(argv) == 1
    assert NEW_MODULE in capsys.readouterr().out


def test_a_pull_request_only_annotates_a_bad_weight_outside_its_diff(tmp_path, capsys):
    """The pass half: the same report, with the module outside the diff. Most PRs change no
    test module a shard ran, so the in-diff half is empty and must not read as a report
    whose format changed."""
    changed = tmp_path / "changed.txt"
    changed.write_text("README.md\n")
    log = tmp_path / "durations.log"
    log.write_text(REPORT)
    argv = ["--check-durations", str(log), "--changed-files", str(changed)]
    assert pytest_shard.main(argv) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("::warning::") and NEW_MODULE in printed
    assert shard_weight_gate.OUTSIDE_DIFF_NOTE in printed
    assert "parsed no durations" not in printed


def test_a_pull_request_editing_the_weight_table_fails_on_every_module():
    """A PR that re-records weights changes only the table, so a bad row it wrote belongs to a
    module outside its diff. Its author can still repair it, so the gate runs unscoped."""
    changed = {"scripts/dev/pytest_shard_weights.json"}
    problems, _ = shard_weight_gate.gate_complaints(REPORT, RECORDED, changed)
    assert len(problems) == 1 and NEW_MODULE in problems[0]


def test_the_diff_scopes_the_recorded_weight_arms_too():
    """An over-recorded and an under-recorded entry outside the diff annotate as the unweighted
    arm does, and the same two inside it fail."""
    report = (
        "0.09s call     ansible/tests/k8s/test_secret_consumer_census.py::test_every_consumer\n"
        "16.00s call     scripts/dev/tests/test_run_as_cron.py::test_a_shell_builtin_still_runs\n"
    )
    problems, warnings = shard_weight_gate.gate_complaints(report, RECORDED, set())
    assert problems == [] and len(warnings) == 2
    problems, warnings = shard_weight_gate.gate_complaints(
        report, RECORDED, set(RECORDED)
    )
    assert len(problems) == 2 and warnings == []


def test_the_stale_repair_refuses_a_path_no_shard_runs():
    """`--record-files` measures whatever it is handed, so a typo or an unstaged file would
    otherwise write a table row no census ever reads back."""
    with pytest.raises(SystemExit) as caught:
        pytest_shard.record_named_weights(["nope/test_not_a_file.py"])
    assert "nope/test_not_a_file.py" in str(caught.value)


def test_no_mode_names_every_mode(capsys):
    """The usage error lists all seven modes, not the three it named before #4448."""
    with pytest.raises(SystemExit) as exc:
        pytest_shard.main([])
    assert exc.value.code == 64
    err = capsys.readouterr().err
    for mode in (
        "--shard",
        "--summary",
        "--record,",
        "--record-missing",
        "--record-files",
        "--refresh-drifted",
        "--check-durations",
    ):
        assert mode in err.splitlines()[-1], mode
