"""The red/green measures summed over the review records under one or more state directories.

Run: uv run pytest scripts/dev/tests/test_fanout_review_stats.py
"""

import json
from dataclasses import asdict

from fanout_lib.review.review_record import Record
from fanout_lib.review_stats import load, main, summarize


def _test_finding(subkind, confidence=0.8):
    return {"category": "test", "subkind": subkind, "confidence": confidence}


def _write(directory, name, record):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(asdict(record)))


def _records(tmp_path):
    red = Record(
        "1",
        outcome="pr",
        costs={"red": 1.0, "implement": 1.0, "review": 2.0},
        red_gate="passed",
        green_first="unmet",
        red_hunks=4,
        red_hunks_missed=["a.py:3"],
        red_hunks_by_absence=1,
        findings=[_test_finding("vacuous"), _test_finding("scaffold", 0.3)],
    )
    plain = Record(
        "2",
        outcome="needs-input",
        costs={"implement": 2.0},
        base_tests=3,
        base_passing=["t.py::a"],
        findings=[_test_finding("missing-coverage")],
    )
    _write(tmp_path / "claude", "1-20261010T130000Z.json", red)
    _write(tmp_path / "operator", "2-20261010T140000Z.json", plain)
    # Written before #4214's fields existed, and before the --since cut.
    (tmp_path / "operator" / "3-20260901T000000Z.json").write_text(
        json.dumps({"batch": "3", "outcome": "pr", "costs": {"implement": 1.0}})
    )
    (tmp_path / "operator" / "3-stop-hook").mkdir()


def test_the_measures_are_summed_per_group_across_directories(tmp_path):
    _records(tmp_path)
    groups = summarize(load([tmp_path / "claude", tmp_path / "operator"]))
    assert groups["red"]["batches"] == 1
    assert groups["red"]["red_cost_share"] == 0.25
    assert groups["red"]["green_first"] == {"unmet": 1}
    assert groups["red"]["hunks"] == {"tried": 4, "missed": 1, "by_absence": 1}
    assert groups["red"]["test_findings"] == {"vacuous": 1}
    assert groups["plain"]["batches"] == 2
    assert groups["plain"]["base"] == {"batches": 1, "new": 3, "passing": 1}
    assert groups["plain"]["outcomes"] == {"needs-input": 1, "pr": 1}


def test_since_drops_older_records_and_json_prints_the_groups(tmp_path, capsys):
    _records(tmp_path)
    dirs = ["--dir", str(tmp_path / "claude"), "--dir", str(tmp_path / "operator")]
    assert main([*dirs, "--since", "2026-10-01", "--json"]) == 0
    groups = json.loads(capsys.readouterr().out)
    assert groups["plain"]["batches"] == 1
    assert main(dirs) == 0
    assert "red: 1 batches" in capsys.readouterr().out
