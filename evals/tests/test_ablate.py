import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ablate import (
    BASELINE,
    REPO,
    SWEEP_WEEKDAY,
    WHOLE_DOC,
    Budget,
    ablate,
    doc_without,
    rank_docs,
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

CASES = [{"id": "a/1", "agent": "x"}, {"id": "a/2", "agent": "x"}]


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


def test_rank_docs_counts_repo_docs_and_drops_user_level_paths():
    log = [
        "t [s] session_start Project CLAUDE.md",
        "t [s] session_start User /home/u/.claude/CLAUDE.md",
        "t [s] session_start User /home/u/.claude/CLAUDE.md",
        "t [s] bash_path_match Project roles/x/CLAUDE.md trigger=roles/x/a.py",
        "t [s] path_glob_match Project roles/x/CLAUDE.md trigger=roles/x/b.py",
        "malformed",
    ]
    assert rank_docs(log) == [("roles/x/CLAUDE.md", 2), ("CLAUDE.md", 1)]


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
    # $4 + $4 spent; the third run projects at $6 and would reach $14.
    assert len(calls) == 2
    assert run["cut_arm"] == WHOLE_DOC
    assert "would take $8.00 past the $10.00 cap" in run["stopped"]
    summary = summarize(run, [BASELINE, WHOLE_DOC], CASES, 1)
    assert summary["measured"] == {}
    assert summary["unmeasured"] == [WHOLE_DOC]


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
    assert alpha["cases"]["a/2"] == {"with": "1/1", "without": "0/1", "changed": True}
    assert summary["measured"][WHOLE_DOC]["changed"] is False


def test_an_unknown_section_is_refused():
    with pytest.raises(KeyError):
        doc_without(DOC, "Gamma")
