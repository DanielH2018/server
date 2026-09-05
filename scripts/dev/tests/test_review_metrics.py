"""Red-proof pair for scripts/dev/review_metrics.py's rate maths.

Run: uv run pytest scripts/dev/tests/test_review_metrics.py
"""

import review_metrics as rm


def test_false_positive_rate_computes_from_complete_counts():
    row = {"high": 2, "medium": 6, "low": 0, "refuted": 2}
    assert rm.false_positive_rate(row) == 2 / 10


def test_false_positive_rate_is_null_on_a_missing_count():
    row = {"high": 2, "medium": None, "low": 0, "refuted": 2}
    assert rm.false_positive_rate(row) is None


def test_fix_refusal_rate_computes_from_complete_counts():
    row = {"fixes_proposed": 8, "fixes_refuted": 3}
    assert rm.fix_refusal_rate(row) == 3 / 8


def test_fix_refusal_rate_is_null_when_nothing_was_proposed():
    row = {"fixes_proposed": 0, "fixes_refuted": 0}
    assert rm.fix_refusal_rate(row) is None


def test_fix_refusal_rate_is_null_on_a_missing_count():
    row = {"fixes_proposed": 8, "fixes_refuted": None}
    assert rm.fix_refusal_rate(row) is None


def test_build_table_carries_date_and_ledger_through():
    rows = [
        {
            "date": "2026-09-01",
            "ledger": "review-2026-09-01-state",
            "high": 0,
            "medium": 5,
            "low": 11,
            "refuted": 2,
            "fixes_proposed": 8,
            "fixes_refuted": 3,
        }
    ]
    table = rm.build_table(rows)
    assert table[0]["date"] == "2026-09-01"
    assert table[0]["ledger"] == "review-2026-09-01-state"
    assert table[0]["false_positive_rate"] == 2 / 18
    assert table[0]["fix_refusal_rate"] == 3 / 8


def test_validate_row_accepts_a_complete_row():
    good = {
        "date": "2026-09-01",
        "high": 0,
        "medium": 5,
        "low": 11,
        "refuted": 2,
        "downgraded": None,
        "fixes_proposed": 8,
        "fixes_confirmed_safe": 5,
        "fixes_refuted": 3,
        "prs": [685, 686],
        "ledger": "review-2026-09-01-state",
    }
    assert rm.validate_row(good) == []


def test_validate_row_flags_missing_field_and_wrong_types():
    bad = {
        "date": 20260901,
        "high": "zero",
        "medium": 5,
        "low": 11,
        "refuted": 2,
        "downgraded": None,
        "fixes_proposed": 8,
        "fixes_confirmed_safe": 5,
        "fixes_refuted": 3,
        "prs": ["685"],
        "ledger": 1,
    }
    problems = rm.validate_row(bad)
    assert any("date is not a string" in p for p in problems)
    assert any("high is not an int" in p for p in problems)
    assert any("prs is not a list" in p for p in problems)
    assert any("ledger is not a string" in p for p in problems)
