"""Sum the fan-out review records into what the review costs and what it finds.

- `outcomes`: how the batches ended.
- `base`: the PR's new tests that pass with its code changes taken out
  (`fanout_lib.review.base_check`).
- `test_findings`: the reviewer's `test` findings at the confidence floor or more, by subkind.

A record keeps the keys its pipeline wrote, and a key this module does not name is not read.

The records live under each user's `~/.local/state/fanout-review/`, so pass `--dir` once per
user whose batches should count.

Usage::

    fanout.py stats [--dir DIR ...] [--since YYYY-MM-DD] [--json]
"""

import json
import re
from collections import Counter
from pathlib import Path


from dev.fanout_lib.review.api import CONFIDENCE_FLOOR, STATE_DIR
from lib.cli_help import ArgumentParser

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


def summarize(rows: list[dict]) -> dict:
    """The measures above over `rows`."""
    spend = sum(sum((r.get("costs") or {}).values()) for r in rows)
    findings = Counter(
        f.get("subkind") or "unspecified"
        for r in rows
        for f in r.get("findings") or []
        if f.get("category") == "test"
        and float(f.get("confidence") or 0) >= CONFIDENCE_FLOOR
    )
    # A `place resume` unit saves a record of its own for a batch already counted.
    batches = [r for r in rows if not r.get("resumed")]
    return {
        "batches": len(batches),
        "outcomes": dict(Counter(r.get("outcome") or "unknown" for r in batches)),
        "mean_cost": round(spend / len(batches), 2) if batches else 0.0,
        "base": {
            "batches": sum(1 for r in rows if r.get("base_tests")),
            "new": sum(r.get("base_tests") or 0 for r in rows),
            "passing": sum(len(r.get("base_passing") or []) for r in rows),
        },
        "test_findings": dict(findings),
    }


def render(m: dict) -> str:
    if not m["batches"]:
        return "no review records"
    base = m["base"]
    return "\n".join(
        [
            f"{m['batches']} batches, mean cost ${m['mean_cost']}",
            f"  outcomes: {_counts(m['outcomes'])}",
            f"  new tests passing without the fix: {base['passing']} of {base['new']} "
            f"in {base['batches']} batches",
            f"  test findings at confidence {CONFIDENCE_FLOOR}+: "
            f"{_counts(m['test_findings'])}",
        ]
    )


def _counts(counts: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"


def main(argv=None) -> int:
    p = ArgumentParser(
        prog="fanout.py stats", description=__doc__.splitlines()[0], allow_abbrev=False
    )
    p.add_argument(
        "--dir",
        action="append",
        type=Path,
        help=f"a state directory of review records (default {STATE_DIR}); repeatable",
    )
    p.add_argument(
        "--since", default="", help="only records saved on or after YYYY-MM-DD"
    )
    p.add_argument("--json", action="store_true", help="print the measures as JSON")
    args = p.parse_args(argv)
    measures = summarize(load(args.dir or [STATE_DIR], args.since))
    print(json.dumps(measures) if args.json else render(measures))
    return 0
