"""The fan-out batches that stopped for a person, and the line each agent stopped on (#7686).

WHY. A quarter of fan-out batches end `needs-input` or `failed`, and the review record kept only
that outcome. Over 2026-10-25 to 2026-11-08, 17 of the 49 `needs-input` batches gave no reason
any record field showed. `review_record.Record.blocker` keeps the line the agent's report ended
on, and this module lists those lines, so the reasons can be sorted.

The list reads every record in the window, not only those of merged PRs, because most such
batches merged nothing. `pipeline_timeline.py` prints it after its PR rows.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.review.api import RECORD_NAME, SAVED_FORMAT

STOPPED = ("needs-input", "failed")
# The blocker line as the summary prints it.
LINE_CHARS = 120


def load_stops(directory: Path, since: datetime) -> list[dict]:
    """Each batch saved on or after `since` that ended `needs-input` or `failed`.

    `blocker` is the line the agent stopped on, "" in a record saved before #7686.
    """
    out = []
    for path in sorted(directory.glob("*.json")):
        name = RECORD_NAME.match(path.name)
        if not name:
            continue
        saved = datetime.strptime(name["saved"], SAVED_FORMAT).replace(tzinfo=UTC)
        if saved < since:
            continue
        try:
            record = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        if record.get("outcome") in STOPPED:
            out.append(
                {
                    "batch": name["batch"],
                    "outcome": record["outcome"],
                    "blocker": str(record.get("blocker") or ""),
                }
            )
    return out


def stop_lines(stops: list[dict]) -> list[str]:
    """The stopped batches' count by outcome, then each one's blocker line."""
    if not stops:
        return ["batches stopped for a person: 0"]
    counts = {o: sum(1 for s in stops if s["outcome"] == o) for o in STOPPED}
    silent = sum(1 for s in stops if not s["blocker"])
    lines = [
        f"batches stopped for a person: {len(stops)} ("
        + ", ".join(f"{o} {n}" for o, n in counts.items())
        + f"), {silent} with no blocker line"
    ]
    lines.extend(
        f"  {s['batch']} {s['outcome']}: {s['blocker'][:LINE_CHARS]}"
        for s in stops
        if s["blocker"]
    )
    return lines
