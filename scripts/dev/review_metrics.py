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
import sys as _sys
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.repo_paths import REPO

OUTCOMES = REPO / "evals" / "review_outcomes.jsonl"

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


# --- coverage ledger -------------------------------------------------------------------
#
# One file per review run under evals/review_coverage/<date>.json: a JSON array with one row
# per review domain. The row says what the domain's agent did — returned findings, returned an
# explicit clean, or returned nothing — so "no finding here" and "nobody looked here" are
# different rows rather than the same silence. Adapted from the coverage ledger in
# cloudflare/security-audit-skill, cut down to homelab-review's own unit, the domain.
COVERAGE_DIR = REPO / "evals" / "review_coverage"

# The six areas homelab-review's step 1 dispatches, by the name findings.py --domain uses.
# A domain renamed in the skill fails here by name instead of validating an empty set.
REVIEW_DOMAINS = frozenset(
    {"security", "network", "backup-observability", "cicd", "container", "docs"}
)

# covered: the agent returned findings or leads. clean: it returned an explicit "nothing found"
# for the paths it names. hole: it returned nothing, errored, or was never dispatched — never
# a passing grade. out_of_scope: the operator scoped the run to a subset (step 1).
COVERAGE_STATUSES = frozenset({"covered", "clean", "hole", "out_of_scope"})

_COVERAGE_REQUIRED = (
    "date",
    "domain",
    "agent",
    "status",
    "reviewed",
    "findings",
    "leads",
)


def validate_coverage(rows: list[dict]) -> list[str]:
    """Return the schema problems in one run's coverage ledger, or [] if it's clean.

    A ledger is one row per REVIEW_DOMAINS member. A `covered` row names what it reviewed and
    carries at least one finding or lead; a `clean` row names what it reviewed and carries
    none; a `hole` row carries a `reason` and no evidence; an `out_of_scope` row carries none.
    """
    problems = []
    if not isinstance(rows, list):
        return ["ledger is not a list"]
    seen: dict[str, int] = {}
    for i, row in enumerate(rows):
        base = f"row {i}"
        if not isinstance(row, dict):
            problems.append(f"{base}: not an object")
            continue
        for field in _COVERAGE_REQUIRED:
            if field not in row:
                problems.append(f"{base}: missing field: {field}")
        domain = row.get("domain")
        if domain not in REVIEW_DOMAINS:
            problems.append(f"{base}: unknown domain: {domain!r}")
        else:
            seen[domain] = seen.get(domain, 0) + 1
        status = row.get("status")
        if status not in COVERAGE_STATUSES:
            problems.append(f"{base}: unknown status: {status!r}")
            continue
        for field in ("reviewed", "findings", "leads"):
            if field in row and not (
                isinstance(row[field], list)
                and all(isinstance(x, str) for x in row[field])
            ):
                problems.append(f"{base}: {field} is not a list of strings")
        reviewed = row.get("reviewed") or []
        evidence = (row.get("findings") or []) + (row.get("leads") or [])
        if status in ("covered", "clean") and not reviewed:
            problems.append(f"{base}: {status} row names nothing it reviewed")
        if status == "covered" and not evidence:
            problems.append(f"{base}: covered row carries no finding or lead")
        if status in ("clean", "hole", "out_of_scope") and evidence:
            problems.append(f"{base}: {status} row carries findings or leads")
        if status == "hole" and not row.get("reason"):
            problems.append(f"{base}: hole row has no reason")
    for domain in sorted(REVIEW_DOMAINS):
        n = seen.get(domain, 0)
        if n == 0:
            problems.append(f"domain absent from the ledger: {domain}")
        elif n > 1:
            problems.append(f"domain appears {n} times: {domain}")
    return problems


def load_coverage(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    parser.add_argument(
        "--check-coverage",
        type=Path,
        metavar="LEDGER",
        help="validate one evals/review_coverage/<date>.json ledger and exit 1 on a problem",
    )
    args = parser.parse_args()
    if args.check_coverage:
        problems = validate_coverage(load_coverage(args.check_coverage))
        for p in problems:
            print(p, file=_sys.stderr)
        raise SystemExit(1 if problems else 0)
    table = build_table(load_outcomes())
    if args.json:
        print(json.dumps(table, indent=2))
    else:
        print(format_table(table))


if __name__ == "__main__":
    main()
