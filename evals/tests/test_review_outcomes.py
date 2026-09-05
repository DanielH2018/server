"""Schema guard for evals/review_outcomes.jsonl, one row per dated /homelab-review run.

Run: uv run pytest evals/tests/test_review_outcomes.py

Offline and free: this only checks each row's shape, matching the
`scripts/validate/tests/test_validate_compose_templates.py` pattern of proving the guard
via review_metrics.validate_row rather than trusting the committed file to stay clean.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "dev"))

from review_metrics import validate_row

OUTCOMES = Path(__file__).resolve().parents[1] / "review_outcomes.jsonl"

_GOOD_ROW = {
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


def _rows() -> list[dict]:
    return [
        json.loads(line)
        for line in OUTCOMES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_validate_row_accepts_a_good_row():
    assert validate_row(_GOOD_ROW) == []


def test_validate_row_flags_a_missing_field():
    bad = dict(_GOOD_ROW)
    del bad["refuted"]
    assert any("missing field: refuted" in p for p in validate_row(bad))


def test_review_outcomes_file_is_non_vacuous_and_valid():
    rows = _rows()
    assert len(rows) >= 4, (
        "evals/review_outcomes.jsonl should carry at least the four backfilled runs"
    )
    dates = {r.get("date") for r in rows}
    assert {"2026-08-31", "2026-09-01"} <= dates, (
        "the two source ledgers should each have produced a row"
    )
    for row in rows:
        problems = validate_row(row)
        assert not problems, f"{row.get('date')}: {problems}"
