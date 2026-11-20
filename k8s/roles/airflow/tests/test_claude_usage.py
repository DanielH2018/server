"""The Claude Code usage rollup's per-session arithmetic and its check, against a recorded day.

`fixtures/claude-2026-11-14-cost.json` is `usage_query`'s range result for the cost counter
over 2026-11-14 UTC, and `...-cost-baseline.json` is `baseline_query`'s result. Each keeps
six series: three sessions that began before the day and three that began in it. The scrub
kept only the labels the rollup reads, with each session id replaced.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from dags.homelab_warehouse import claude_usage as cu
from dags.homelab_warehouse import prom_rollup as pr

DAY = date(2026, 11, 14)
WINDOW = pr.DayWindow.for_day(DAY)
FIXTURES = Path(__file__).parent / "fixtures"
COST = next(metric for metric in cu.METRICS if metric.metric == "cost_usd")


def recorded(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"claude-2026-11-14-{name}.json").read_text())


def cost_rows(baselines=None) -> dict[tuple, float]:
    if baselines is None:
        baselines = pr.baselines_by_series(recorded("cost-baseline"))
    rows = cu.usage_rows(COST, WINDOW, recorded("cost"), baselines)
    return {row[2:5]: row[6] for row in rows}


def series(*values: float) -> dict:
    return {
        "metric": {},
        "values": [[WINDOW.start + 300 * i, str(v)] for i, v in enumerate(values)],
    }


def test_a_session_from_an_earlier_day_counts_only_its_rise_since_its_baseline():
    rows = cost_rows()

    # Sessions 1 and 3 rose from 6.0529 to 27.2959 and from 2.8230 to 12.8308.
    assert rows[("daniel-server", "sdk-cli", "claude-opus-5-5")] == pytest.approx(
        31.2508, abs=1e-3
    )
    assert rows[("daniel-server", "sdk-cli", "claude-sonnet-5-5")] == pytest.approx(
        2.2907, abs=1e-3
    )


def test_a_session_that_began_in_the_day_counts_from_zero():
    rows = cost_rows()

    # Sessions 4, 5 and 6 ended the day at 2.8305, 0.3728 and 0.5195.
    assert rows[("daniel-box", "claude-desktop", "claude-opus-5-5")] == pytest.approx(
        3.7228, abs=1e-3
    )


def test_a_counter_that_drops_counts_up_again_from_zero():
    assert pr.series_increase(series(5, 7, 1, 3), baseline=5) == 2 + 1 + 2


def test_spend_without_tokens_and_a_negative_row_fail_the_check():
    rows = [
        {"metric": "cost_usd", "value": 3.0},
        {"metric": "sessions", "value": -1.0},
    ]

    assert cu.quality_problems(DAY, rows) == [
        f"{DAY}: sessions has a negative row",
        f"{DAY}: cost and tokens disagree on whether Claude Code ran",
    ]


def test_tokens_without_spend_fail_the_check():
    rows = [{"metric": "tokens", "value": 1200.0}]

    assert cu.quality_problems(DAY, rows) == [
        f"{DAY}: cost and tokens disagree on whether Claude Code ran"
    ]


def test_a_day_with_no_claude_code_use_passes_the_check():
    assert cu.quality_problems(DAY, []) == []
