import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ablate_budget import complete_arms, fitting_selection, report_entries, run_costs


def test_a_run_costs_its_entry_over_k_and_the_costliest_run_wins(tmp_path: Path):
    (tmp_path / "sweep").mkdir()
    (tmp_path / "sweep" / "agent.json").write_text(
        json.dumps([{"id": "a/1", "k": 3, "costUsd": 0.3}, {"id": "a/2", "k": 1}])
    )
    (tmp_path / "run.json").write_text(
        json.dumps([{"id": "a/1", "k": 1, "costUsd": 0.2}])
    )
    # An ablation report.json is a dict, not an engine report, and is skipped.
    (tmp_path / "report.json").write_text(json.dumps({"raw": {}}))
    assert run_costs(report_entries([tmp_path, tmp_path / "absent"])) == {"a/1": 0.2}


def test_a_plan_is_played_through_the_runners_own_budget_stop():
    # 30 cases at k=3 and $0.08 a run: $7.20 an arm, past the $6.25 the floor leaves.
    assert complete_arms([0.08] * 30, 3, 15, 10.0) == 0
    # 3 cases: $0.72 an arm, so 8 arms ($5.76) launch under $6.25 and a ninth does not.
    assert complete_arms([0.08] * 3, 3, 15, 10.0) == 8


def test_the_fitting_selection_takes_cases_cheapest_first_then_sections():
    per_run = {"a/1": 1.0, "a/2": 0.1, "a/3": 0.1}
    cases, sections = fitting_selection(list(per_run), per_run, 3, ["X", "Y"], 10.0)
    # a/2 and a/3 cost $0.60 an arm. Adding a/1 makes $3.60 an arm, and the third arm's
    # runs stop launching once $6.25 is spent.
    assert cases == ["a/2", "a/3"]
    assert sections == ["X", "Y"]
    assert fitting_selection(["a/1"], {"a/1": 1.0}, 3, ["X"], 10.0) == ([], [])
