"""Sum the fan-out review records into the measures that decide whether the red phase stays.

#3674 said the red phase "has to show catches to stay" and counted a red-gate refusal as a
catch. The test author reruns its tests until they fail, so refusals sit near zero whatever
the tests are worth. These are the measures that can show a catch instead, per group of
batches: `red` (the red gate passed), `red-refused` (a red phase ran and was refused) and
`plain` (no red phase).

- `green_first`: how the first green gate run ended. `unmet` is the catch: the implementer's
  first attempt did not do what the red tests ask. `flaky` is a red node that passed once
  and failed a repeat, and `lock` is a refusal about what the fix touched; neither is a
  catch.
- `hunks`: fix hunks reverted one at a time under the red tests (`fanout_lib.hunk_check`),
  how many no red test noticed, and how many were noticed only through a missing name.
- `base`: the PR's new tests that pass with its code changes taken out
  (`fanout_lib.base_check`), which every batch measures, red phase or not.
- `test_findings`: the reviewer's `test` findings at confidence 0.6 or more, by subkind.
- `red_cost_share`: the red phase's share of the group's spend.

The records live under each user's `~/.local/state/fanout-review/`, so pass `--dir` once per
user whose batches should count.

Usage::

    fanout_review_stats.py [--dir DIR ...] [--since YYYY-MM-DD] [--json]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.review import STATE_DIR
from fanout_lib.review_record import CONFIDENCE_FLOOR

# `<batch>-<YYYYMMDDTHHMMSSZ>.json`, as `Pipeline._save` names a record.
_STAMP = re.compile(r"-(\d{8})T\d{6}Z\.json$")


def load(dirs: list[Path], since: str = "") -> list[dict]:
    """Every readable record under `dirs` saved on or after `since` (`YYYY-MM-DD`)."""
    cut = since.replace("-", "")
    records = []
    for directory in dirs:
        for path in sorted(directory.glob("*.json")):
            stamp = _STAMP.search(path.name)
            if not stamp or stamp.group(1) < cut:
                continue
            try:
                record = json.loads(path.read_text())
            except OSError, ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def group_of(record: dict) -> str:
    red = record.get("red_gate") or ""
    if not red:
        return "plain"
    return "red" if red == "passed" else "red-refused"


def summarize(records: list[dict]) -> dict[str, dict]:
    """The measures above for each group that has at least one record."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(group_of(record), []).append(record)
    return {name: _measures(rows) for name, rows in sorted(groups.items())}


def _measures(rows: list[dict]) -> dict:
    spend = sum(sum((r.get("costs") or {}).values()) for r in rows)
    red_spend = sum((r.get("costs") or {}).get("red", 0) for r in rows)
    findings = Counter(
        f.get("subkind") or "unspecified"
        for r in rows
        for f in r.get("findings") or []
        if f.get("category") == "test"
        and float(f.get("confidence") or 0) >= CONFIDENCE_FLOOR
    )
    return {
        "batches": len(rows),
        "outcomes": dict(Counter(r.get("outcome") or "unknown" for r in rows)),
        "mean_cost": round(spend / len(rows), 2),
        "red_cost_share": round(red_spend / spend, 2) if spend else 0.0,
        "green_first": dict(
            Counter(r["green_first"] for r in rows if r.get("green_first"))
        ),
        "hunks": {
            "tried": sum(r.get("red_hunks") or 0 for r in rows),
            "missed": sum(len(r.get("red_hunks_missed") or []) for r in rows),
            "by_absence": sum(r.get("red_hunks_by_absence") or 0 for r in rows),
        },
        "base": {
            "batches": sum(1 for r in rows if r.get("base_tests")),
            "new": sum(r.get("base_tests") or 0 for r in rows),
            "passing": sum(len(r.get("base_passing") or []) for r in rows),
        },
        "test_findings": dict(findings),
    }


def render(groups: dict[str, dict]) -> str:
    lines = []
    for name, m in groups.items():
        hunks, base = m["hunks"], m["base"]
        lines += [
            f"{name}: {m['batches']} batches, mean cost ${m['mean_cost']}, "
            f"red phase {m['red_cost_share']:.0%} of spend",
            f"  outcomes: {_counts(m['outcomes'])}",
            f"  first green gate: {_counts(m['green_first'])}",
            f"  fix hunks under the red tests: {hunks['tried']} tried, {hunks['missed']} "
            f"unnoticed, {hunks['by_absence']} noticed only through a missing name",
            f"  new tests passing without the fix: {base['passing']} of {base['new']} "
            f"in {base['batches']} batches",
            f"  test findings at confidence {CONFIDENCE_FLOOR}+: "
            f"{_counts(m['test_findings'])}",
        ]
    return "\n".join(lines) or "no review records"


def _counts(counts: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    p.add_argument(
        "--dir",
        action="append",
        type=Path,
        help=f"a state directory of review records (default {STATE_DIR}); repeatable",
    )
    p.add_argument(
        "--since", default="", help="only records saved on or after YYYY-MM-DD"
    )
    p.add_argument("--json", action="store_true", help="print the groups as JSON")
    args = p.parse_args(argv)
    groups = summarize(load(args.dir or [STATE_DIR], args.since))
    print(json.dumps(groups, indent=2) if args.json else render(groups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
