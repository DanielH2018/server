"""Outcome carries verdict and exit code together, and refuses the shapes that mis-count.

Run: uv run pytest scripts/deploy_tools/tests/test_land_outcome.py
"""

from __future__ import annotations

import pytest

from deploy_tools.land_lib import outcome


def test_an_exit_75_outcome_must_name_a_verdict():
    with pytest.raises(ValueError):
        outcome.Outcome(75, "gave up")
    assert outcome.Outcome(75, "gave up", "lock-busy").verdict == "lock-busy"


def test_an_unknown_verdict_is_refused():
    with pytest.raises(ValueError):
        outcome.Outcome(1, "x", "not-a-verdict")


def test_emit_prints_the_error_to_stderr_and_the_verdict_to_stdout(capsys):
    outcome.Outcome(1, "PR #7 — CI is red", "ci-red", error="CI is red").emit()
    cap = capsys.readouterr()
    assert cap.err == "land: CI is red\n"
    assert cap.out == "VERDICT: ci-red (PR #7 — CI is red)\n"


def test_a_verdictless_outcome_prints_no_verdict_line(capsys):
    outcome.Outcome(2, "PR #7 — bad", error="bad").emit()
    assert "VERDICT" not in capsys.readouterr().out
