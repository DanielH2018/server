"""M6 Open question 9 records an `ALERTS_FOR_STATE` reading before and after a Prometheus restart.

The question decides whether `probe.py health` keeps its "age is unknown" message
(`scripts/diagnostics/probe_lib/health_alerts.py:judge_alerts`). B-1 recorded one reading on
2026-10-16: the Watchdog's `ALERTS_FOR_STATE` read 1792078185, 6.4s after
`process_start_time_seconds{job="prometheus"}` read 1792078178.57. One reading cannot show
whether a restart moves the value, so the before/after pair is owed (#5333).

The oracle is time order, taken from the issue rather than from the doc. A reading taken across
a restart after B-1 carries an epoch later than B-1's `ALERTS_FOR_STATE` value: a newer process
start time, or a reset activation time. A before/after pair is at least two epochs at or after
that value. The test does not exclude B-1's value itself, because the Watchdog fires without a
break and its "before" reading may legitimately repeat it.

Run: uv run pytest ansible/tests/repo/test_m6_alerts_for_state_restart_reading.py
"""

import re

from _markdown_section import section, steps
from lib.repo_paths import REPO

DOC = REPO / "docs" / "target-architecture" / "monitoring" / "m6-retire-kuma.md"
QUESTION = 9

# B-1's `ALERTS_FOR_STATE` reading of 2026-10-16, quoted in #5333.
B1_ALERTS_FOR_STATE = 1792078185

# A Unix epoch in seconds from 2026 on: ten digits, an optional fraction.
_EPOCH = re.compile(r"(?<![\d.])(1[7-9]\d{8})(?:\.\d+)?(?![\d])")


def _question() -> str:
    entry = steps(section(DOC.read_text(), "## Open questions")).get(QUESTION, "")
    assert "ALERTS_FOR_STATE" in entry, (
        f"Open question {QUESTION} of {DOC.name} is missing or no longer about ALERTS_FOR_STATE"
    )
    return entry


def _epochs_since_b1() -> list[float]:
    return [
        float(m.group(0))
        for m in _EPOCH.finditer(_question())
        if float(m.group(0)) >= B1_ALERTS_FOR_STATE
    ]


def test_question_records_a_reading_taken_after_b1():
    later = [e for e in _epochs_since_b1() if e > B1_ALERTS_FOR_STATE]
    assert later, (
        f"Open question {QUESTION} records no epoch later than B-1's ALERTS_FOR_STATE "
        f"{B1_ALERTS_FOR_STATE}, so no reading across a restart after B-1 is recorded"
    )


def test_question_records_a_before_and_an_after_reading():
    readings = _epochs_since_b1()
    assert len(readings) >= 2, (
        f"Open question {QUESTION} records {len(readings)} reading(s) at or after B-1's "
        f"ALERTS_FOR_STATE {B1_ALERTS_FOR_STATE}; a before/after pair needs two"
    )


# One labelled reading of the pair: the alert's activation and Prometheus's start at that time.
_READING = re.compile(
    r"- (Before|After) the restart: `ALERTS_FOR_STATE` `(\d+(?:\.\d+)?)`,\s+"
    r"`process_start_time_seconds`\s+`(\d+(?:\.\d+)?)`"
)


def _pair() -> dict[str, tuple[float, float]]:
    pair = {
        m.group(1): (float(m.group(2)), float(m.group(3)))
        for m in _READING.finditer(_question())
    }
    assert set(pair) == {"Before", "After"}, (
        f"Open question {QUESTION} records no labelled before/after ALERTS_FOR_STATE pair; "
        f"found {sorted(pair)}"
    )
    return pair


def test_the_pair_straddles_a_restart_after_b1():
    pair = _pair()
    before_active, before_start = pair["Before"]
    after_active, after_start = pair["After"]
    assert B1_ALERTS_FOR_STATE <= before_start < after_start, (
        "the before and after readings must come from two Prometheus processes started after B-1"
    )
    assert before_start <= before_active < after_start <= after_active, (
        "each ALERTS_FOR_STATE value must fall inside the process it was read from"
    )


def test_the_recorded_verdict_matches_the_pair():
    pair = _pair()
    before, after = pair["Before"][0], pair["After"][0]
    verdict = "the value moves" if after != before else "the value does not move"
    assert verdict in _question(), (
        f"the pair reads {before} then {after}, so Open question {QUESTION} must say {verdict!r}"
    )
