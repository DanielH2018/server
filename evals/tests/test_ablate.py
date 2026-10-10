import json
import os
import re
import shlex
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ablate import (
    BASELINE,
    EXIT_DONE,
    EXIT_REFUSED,
    REPO,
    SWEEP_WEEKDAY,
    WHOLE_DOC,
    WORST_RUN_USD,
    Budget,
    ablate,
    doc_without,
    load_cases,
    main,
    split_sections,
    summarize,
    sweep_day_conflict,
    with_doc,
)

DOC = """# Title
Intro line.

## Alpha
Alpha body.
### Alpha detail
More alpha.

## Beta
```bash
## not a heading, inside a fence
```
Beta body.
"""

AGENT = "---\nname: skeptic\ndescription: d\n---\nYou verify findings.\n"

CASES = [
    {"id": "a/1", "agent": "x", "threshold": "all"},
    {"id": "a/2", "agent": "x", "threshold": "all"},
]


def _arms(*names):
    return [(n, {"x": AGENT}) for n in names]


def _invoker(costs, passes=1):
    """Returns an engine stand-in billing `costs` in order, and the list of calls made."""
    calls = []

    def invoke(case_id, env, arm_dir):
        calls.append((arm_dir.name, case_id))
        return {"passes": passes, "healthy": 1, "costUsd": costs[len(calls) - 1]}

    return invoke, calls


def test_sections_split_on_level_two_headings_outside_fences():
    preamble, sections = split_sections(DOC)
    assert preamble == "# Title\nIntro line.\n\n"
    assert [h for h, _ in sections] == ["Alpha", "Beta"]
    assert "### Alpha detail" in sections[0][1]
    assert "## not a heading" in sections[1][1]


def test_an_ablated_prompt_differs_from_the_baseline_only_by_the_section():
    alpha = split_sections(DOC)[1][0][1]
    baseline = with_doc(AGENT, "CLAUDE.md", DOC)
    ablated = with_doc(AGENT, "CLAUDE.md", doc_without(DOC, "Alpha"))
    assert ablated != baseline
    assert baseline.replace(alpha, "", 1) == ablated
    assert baseline.startswith("---\nname: skeptic\n")


def test_the_whole_doc_arm_is_the_agent_file_unchanged():
    assert with_doc(AGENT, "CLAUDE.md", None) == AGENT


def test_sweep_weekday_matches_the_cron():
    crons = (REPO / "ansible/roles/setup/initial_setup/tasks/crons.yml").read_text()
    block = crons.split('name: "Homelab eval sweep"', 1)[1]
    m = re.search(r'weekday: "(\d)"', block)
    assert m, "the eval sweep cron no longer names a weekday"
    cron_weekday = int(m.group(1))
    # Cron counts Sunday as 0; Python's weekday() counts Monday as 0.
    assert (cron_weekday - 1) % 7 == SWEEP_WEEKDAY


def test_refuses_the_sweep_weekday_and_a_day_with_a_recorded_sweep():
    sunday = date(2026, 10, 11)
    monday = date(2026, 10, 12)
    assert sunday.weekday() == SWEEP_WEEKDAY
    assert sweep_day_conflict(sunday, {}) is not None
    assert sweep_day_conflict(monday, {}) is None
    noon = datetime(2026, 10, 12, 12).astimezone().timestamp()
    assert sweep_day_conflict(monday, {"_runs": [{"ts": noon}]}) is not None


def test_stops_before_a_run_projected_to_cross_the_cap(tmp_path: Path):
    invoke, calls = _invoker([4.0] * 10)
    run = ablate(_arms(BASELINE, WHOLE_DOC), CASES, 1, Budget(10.0), invoke, tmp_path)
    # $4 + $4 spent; the third run projects at 1.5x $4, above the floor, and would reach $14.
    assert len(calls) == 2
    assert run["cut_arm"] == WHOLE_DOC
    assert "would take $8.00 past the $10.00 cap" in run["stopped"]
    summary = summarize(run, [BASELINE, WHOLE_DOC], CASES, 1)
    assert summary["measured"] == {}
    assert summary["unmeasured"] == [WHOLE_DOC]


def test_a_run_launches_only_when_its_worst_case_still_fits(tmp_path: Path):
    invoke, calls = _invoker([0.2] * 10)
    run = ablate(_arms(BASELINE, WHOLE_DOC), CASES, 1, Budget(4.0), invoke, tmp_path)
    # $0.40 spent; a third run could bill $3.75 more and reach $4.15.
    assert WORST_RUN_USD == 3.75
    assert len(calls) == 2
    assert "projected at $3.75 would take $0.40 past the $4.00 cap" in run["stopped"]


def test_a_report_without_cost_stops_because_its_spend_is_unknown(tmp_path: Path):
    run = ablate(
        _arms(BASELINE),
        CASES,
        1,
        Budget(10.0),
        lambda *_: {"passes": 1, "healthy": 1},
        tmp_path,
    )
    assert run["stopped"] == "the report for a/1 carries no costUsd; spend unknown"


def test_live_cases_are_left_out_because_the_engine_skips_them(tmp_path: Path):
    (tmp_path / "x").mkdir()
    for cid, mode in (("x/1", None), ("x/2", "live")):
        case = {"id": cid, "agent": "x", **({"mode": mode} if mode else {})}
        (tmp_path / f"{cid}.json").write_text(json.dumps(case))
    assert [c["id"] for c in load_cases([], [], tmp_path)] == ["x/1"]
    with pytest.raises(KeyError):
        load_cases(["x/2"], [], tmp_path)


def test_a_move_within_the_case_threshold_is_not_a_change(tmp_path: Path):
    passes = iter([3, 2])  # baseline 3/3, -doc 2/3: both meet rate>=2/3

    def invoke(case_id, env, arm_dir):
        return {"passes": next(passes), "healthy": 3, "costUsd": 0.1}

    cases = [{"id": "a/1", "agent": "x", "threshold": "rate>=2/3"}]
    names = [BASELINE, WHOLE_DOC]
    run = ablate(_arms(*names), cases, 1, Budget(10.0), invoke, tmp_path)
    row = summarize(run, names, cases, 3)["measured"][WHOLE_DOC]
    assert row["cases"]["a/1"]["with"] == "3/3"
    assert row["changed"] is False


def test_stops_as_soon_as_the_measured_spend_crosses_the_cap(tmp_path: Path):
    invoke, calls = _invoker([0.5, 11.0, 0.5, 0.5])
    run = ablate(_arms(BASELINE, WHOLE_DOC), CASES, 1, Budget(10.0), invoke, tmp_path)
    assert len(calls) == 2
    assert run["stopped"] == "spent $11.50, past the $10.00 cap"


def test_reports_each_arm_with_and_without_and_marks_a_flip(tmp_path: Path):
    outcomes = iter([1, 1, 1, 0, 1, 1])  # baseline a/1, a/2; -Alpha a/1, a/2; -doc ...

    def invoke(case_id, env, arm_dir):
        assert env["EVAL_AGENT_DIRS"] == str(arm_dir)
        assert (arm_dir / "x.md").read_text() == AGENT
        return {"passes": next(outcomes), "healthy": 1, "costUsd": 0.1}

    names = [BASELINE, "-Alpha", WHOLE_DOC]
    run = ablate(_arms(*names), CASES, 1, Budget(10.0), invoke, tmp_path)
    assert run["stopped"] is None
    summary = summarize(run, names, CASES, 1)
    assert summary["unmeasured"] == []
    alpha = summary["measured"]["-Alpha"]
    assert alpha["changed"] is True
    assert alpha["cases"]["a/2"] == {
        "with": "1/1",
        "without": "0/1",
        "changed": True,
        "inconclusive": False,
    }
    assert summary["measured"][WHOLE_DOC]["changed"] is False


def test_a_run_with_no_report_stops_because_its_spend_is_unknown(tmp_path: Path):
    reports = iter([{"passes": 1, "healthy": 1, "costUsd": 0.1}, None])
    run = ablate(
        _arms(BASELINE, WHOLE_DOC),
        CASES,
        1,
        Budget(10.0),
        lambda *_: next(reports),
        tmp_path,
    )
    assert run["stopped"] == "the engine wrote no report for a/2; spend unknown"
    assert run["cut_arm"] == BASELINE


def test_an_infra_error_is_inconclusive_rather_than_a_flip(tmp_path: Path):
    healthy = iter([1, 1, 0, 1])  # baseline a/1, a/2; -doc a/1 errored, a/2

    def invoke(case_id, env, arm_dir):
        h = next(healthy)
        return {"passes": h, "healthy": h, "costUsd": 0.1}

    names = [BASELINE, WHOLE_DOC]
    run = ablate(_arms(*names), CASES, 1, Budget(10.0), invoke, tmp_path)
    row = summarize(run, names, CASES, 1)["measured"][WHOLE_DOC]
    assert row["changed"] is False
    assert row["inconclusive"] == ["a/1"]


def test_a_refusal_is_rechecked_before_every_run(tmp_path: Path):
    invoke, calls = _invoker([0.1] * 4)
    refusals = iter([None, "it is Sunday now"])
    run = ablate(
        _arms(BASELINE),
        CASES,
        1,
        Budget(10.0),
        invoke,
        tmp_path,
        refuse=lambda: next(refusals),
    )
    assert len(calls) == 1
    assert run["stopped"] == "it is Sunday now"


def test_an_unknown_section_is_refused():
    with pytest.raises(KeyError):
        doc_without(DOC, "Gamma")


def test_a_default_dry_run_refuses_and_names_a_selection_that_fits(tmp_path, capsys):
    rc = main(["run", "--dry-run", "--cost-reports", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == EXIT_REFUSED
    assert "refusing: the baseline, -doc and one section arm need" in err
    assert "largest selection that fits: --case " in err
    assert "--section " in err


def test_a_plan_that_fits_is_priced_at_the_unpriced_rate_and_accepted(tmp_path, capsys):
    argv = ["run", "--dry-run", "--agent", "skeptic", "--section", "Secrets Management"]
    rc = main(argv + ["--cost-reports", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == EXIT_DONE
    assert "0 of 3 case(s) priced from past reports, the rest at $0.12 a run" in out
    assert "1 of 1 section arm(s) expected to finish" in out


def test_the_printed_selection_is_accepted_when_pasted_back(tmp_path, capsys):
    # The skeptic cases' real prices on 2026-10-10. They sort last in file order and first
    # by cost, so a selection checked cheapest first is refused in the runner's order.
    priced = {
        "001-refuted-with-evidence": 0.097222,
        "002-falsify-the-defense": 0.080694,
    }
    priced["003-no-evidence-is-not-refutation"] = 0.0418034
    entries = [{"id": f"skeptic/{c}", "k": 1, "costUsd": v} for c, v in priced.items()]
    (tmp_path / "runs.json").write_text(json.dumps(entries))
    reports = ["--cost-reports", str(tmp_path)]
    assert main(["run", "--dry-run", *reports]) == EXIT_REFUSED
    line = capsys.readouterr().err.split("largest selection that fits: ", 1)[1]
    flags = shlex.split(line.splitlines()[0])
    assert main(["run", "--dry-run", *reports, *flags]) == EXIT_DONE
    assert "1 of 1 section arm(s) expected to finish" in capsys.readouterr().out
