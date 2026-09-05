#!/usr/bin/env python3
"""Print the /homelab-review outcome trend: false-positive and fix-refusal rates.

Run: uv run python scripts/dev/review_metrics.py [--json]

Reads `evals/review_outcomes.jsonl`, one row per dated review run. The homelab-review
skill's step 7 appends a row here right after it writes the dated ledger memory — see the
skill's own instructions for the exact command.

Two rates trend across runs:

- **False-positive rate** = refuted findings / (confirmed findings + refuted findings),
  where confirmed = high + medium + low. This is the number the skill's step 2 priming
  exists to drive down.
- **Fix-refusal rate** = fixes the fix-skeptic pass refused (UNSAFE or LAUNDERS) / fixes
  proposed. This is the number the skill's step 7 fix-skeptic pass exists to drive down.

A row with an unknown count carries `null` for that field rather than a guessed number, and
a rate that depends on a `null` input is itself `null` — never silently computed from a
partial row.
"""

import argparse
import json
from pathlib import Path

OUTCOMES = Path(__file__).resolve().parents[2] / "evals" / "review_outcomes.jsonl"

REQUIRED_FIELDS = (
    "date",
    "high",
    "medium",
    "low",
    "refuted",
    "downgraded",
    "fixes_proposed",
    "fixes_confirmed_safe",
    "fixes_refuted",
    "prs",
    "ledger",
)
_INT_OR_NULL_FIELDS = (
    "high",
    "medium",
    "low",
    "refuted",
    "downgraded",
    "fixes_proposed",
    "fixes_confirmed_safe",
    "fixes_refuted",
)


def validate_row(obj: dict) -> list[str]:
    """Return the schema problems in one review_outcomes.jsonl row, or [] if it's clean."""
    problems = []
    for field in REQUIRED_FIELDS:
        if field not in obj:
            problems.append(f"missing field: {field}")
    if "date" in obj and not isinstance(obj["date"], str):
        problems.append(f"date is not a string: {obj['date']!r}")
    for field in _INT_OR_NULL_FIELDS:
        if field in obj and obj[field] is not None and not isinstance(obj[field], int):
            problems.append(f"{field} is not an int or null: {obj[field]!r}")
    if "prs" in obj:
        prs = obj["prs"]
        if not isinstance(prs, list) or not all(isinstance(p, int) for p in prs):
            problems.append(f"prs is not a list of ints: {prs!r}")
    if (
        "ledger" in obj
        and obj["ledger"] is not None
        and not isinstance(obj["ledger"], str)
    ):
        problems.append(f"ledger is not a string or null: {obj['ledger']!r}")
    return problems


def load_outcomes(path: Path = OUTCOMES) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def false_positive_rate(row: dict) -> float | None:
    """Refuted findings over (confirmed + refuted) findings for one run.

    Returns None when any of high/medium/low/refuted is unknown, or when the run recorded
    zero findings on either side (an empty ratio, not a zero rate).
    """
    high, medium, low, refuted = (
        row.get("high"),
        row.get("medium"),
        row.get("low"),
        row.get("refuted"),
    )
    if None in (high, medium, low, refuted):
        return None
    denom = high + medium + low + refuted
    if denom == 0:
        return None
    return refuted / denom


def fix_refusal_rate(row: dict) -> float | None:
    """Refused fixes over proposed fixes for one run, or None if either count is unknown."""
    proposed, refused = row.get("fixes_proposed"), row.get("fixes_refuted")
    if not proposed or refused is None:
        return None
    return refused / proposed


def build_table(rows: list[dict]) -> list[dict]:
    return [
        {
            "date": row.get("date"),
            "ledger": row.get("ledger"),
            "false_positive_rate": false_positive_rate(row),
            "fix_refusal_rate": fix_refusal_rate(row),
        }
        for row in rows
    ]


def format_table(table: list[dict]) -> str:
    lines = [f"{'date':<12} {'fp_rate':>8} {'fix_refusal':>12}  ledger"]
    for r in table:
        fp = (
            "n/a"
            if r["false_positive_rate"] is None
            else f"{r['false_positive_rate']:.2f}"
        )
        fr = "n/a" if r["fix_refusal_rate"] is None else f"{r['fix_refusal_rate']:.2f}"
        lines.append(f"{r['date']!s:<12} {fp:>8} {fr:>12}  {r['ledger'] or ''}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the table as JSON instead of text"
    )
    args = parser.parse_args()
    table = build_table(load_outcomes())
    if args.json:
        print(json.dumps(table, indent=2))
    else:
        print(format_table(table))


if __name__ == "__main__":
    main()
