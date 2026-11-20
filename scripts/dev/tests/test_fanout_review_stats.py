"""The measures summed over the review records under one or more state directories.

Run: uv run pytest scripts/dev/tests/test_fanout_review_stats.py
"""

import json
from dataclasses import asdict

from dev.fanout_lib.review.review_record import Record
from dev.fanout_lib.review_stats import load, main, summarize


def _test_finding(subkind, confidence=0.8):
    return {"category": "test", "subkind": subkind, "confidence": confidence}


def _write(directory, name, record):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(record))


def _records(tmp_path):
    landed = Record(
        "1",
        outcome="pr",
        costs={"implement": 1.0, "review": 2.0},
        findings=[_test_finding("vacuous"), _test_finding("scaffold", 0.3)],
    )
    held = Record(
        "2",
        outcome="needs-input",
        costs={"implement": 2.0},
        base_tests=3,
        base_passing=["t.py::a"],
        findings=[_test_finding("missing-coverage")],
    )
    _write(tmp_path / "claude", "1-20261010T130000Z.json", asdict(landed))
    _write(tmp_path / "operator", "2-20261010T140000Z.json", asdict(held))
    # Written before the --since cut, by a pipeline whose record had other keys.
    _write(
        tmp_path / "operator",
        "3-20260901T000000Z.json",
        {
            "batch": "3",
            "outcome": "pr",
            "costs": {"red": 1.0, "implement": 1.0},
            "red_gate": "passed",
            "red_hunks_missed": ["a.py:3"],
        },
    )
    (tmp_path / "operator" / "3-stop-hook").mkdir()


def test_the_measures_are_summed_across_directories(tmp_path):
    _records(tmp_path)
    measures = summarize(load([tmp_path / "claude", tmp_path / "operator"]))
    assert measures == {
        "batches": 3,
        "outcomes": {"needs-input": 1, "pr": 2},
        "mean_cost": 2.33,
        "base": {"batches": 1, "new": 3, "passing": 1},
        "test_findings": {"vacuous": 1, "missing-coverage": 1},
    }


def test_since_drops_older_records_and_json_prints_the_measures(tmp_path, capsys):
    _records(tmp_path)
    dirs = ["--dir", str(tmp_path / "claude"), "--dir", str(tmp_path / "operator")]
    assert main([*dirs, "--since", "2026-10-01", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["batches"] == 2
    assert main(dirs) == 0
    assert capsys.readouterr().out == (
        "3 batches, mean cost $2.33\n"
        "  outcomes: needs-input 1, pr 2\n"
        "  new tests passing without the fix: 1 of 3 in 1 batches\n"
        "  test findings at confidence 0.5+: missing-coverage 1, vacuous 1\n"
    )


def test_no_record_prints_one_line(tmp_path, capsys):
    assert main(["--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "no review records\n"


def test_a_resumed_units_record_adds_its_cost_and_no_batch():
    first = asdict(Record("1", outcome="needs-input", costs={"implement": 3.0}))
    again = asdict(Record("1", outcome="pr", costs={"conflict": 1.0}, resumed=True))
    measures = summarize([first, again])
    assert measures["batches"] == 1
    assert measures["outcomes"] == {"needs-input": 1}
    assert measures["mean_cost"] == 4.0
