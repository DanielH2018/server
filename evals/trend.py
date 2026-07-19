"""Cross-run trend tracking for the homelab eval cases.

The chezmoi eval engine (`run-evals.mjs --json report.json`) writes an array of
per-case aggregates. This rolls those aggregates into `evals/history.json` across
runs, so a case that was reliably passing and suddenly drops is surfaced as a
REGRESSION instead of looking like a one-off flake, oscillating cases show up as
FLAKY, and reliably-passing cases are reported as STABLE (a candidate skip-set).

The engine stays the single source of truth — this only consumes its report.json.
Only HERMETIC runs are trended: subscription runs are too noisy to trend
(see evals/README.md), so `--mode subscription` reports current status without
recording or comparing.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

HISTORY = Path(__file__).parent / "history.json"


def load_history(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return {}


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record_run(history: dict, report: list[dict], ts: int, mode: str) -> dict:
    """Append one entry per case from a report array; returns the updated history."""
    for case in report:
        # INCONCLUSIVE runs carry no pass/fail signal — record the status but leave
        # thresholdMet null so the trend math skips them (mirrors the engine).
        met = None if case.get("status") == "INCONCLUSIVE" else case["thresholdMet"]
        history.setdefault(case["id"], []).append(
            {
                "ts": ts,
                "mode": mode,
                "status": case["status"],
                "passes": case["passes"],
                "healthy": case["healthy"],
                "passRate": case["passRate"],
                "thresholdMet": met,
            }
        )
    return history


def _signal(entries: list[dict], mode: str) -> list[bool]:
    """thresholdMet values for `mode` entries that carry a pass/fail signal, in order."""
    return [
        e["thresholdMet"]
        for e in entries
        if e.get("mode") == mode and e.get("thresholdMet") is not None
    ]


def classify(
    history: dict, *, mode: str = "hermetic", window: int = 5, stable_n: int = 3
) -> dict:
    """Bucket each case by its recent signal history. Categories are exclusive; a
    case needs at least two signal-bearing runs to be classified."""
    out: dict[str, list[str]] = {
        "regressed": [],
        "recovered": [],
        "flaky": [],
        "stable": [],
    }
    for cid in sorted(history):
        sig = _signal(history[cid], mode)
        if len(sig) < 2:
            continue
        cur, prev, recent = sig[-1], sig[-2], sig[-window:]
        if not cur and prev:
            out["regressed"].append(cid)
        elif cur and not prev:
            out["recovered"].append(cid)
        elif len(sig) >= stable_n and all(sig[-stable_n:]):
            out["stable"].append(cid)
        elif True in recent and False in recent:
            out["flaky"].append(cid)
    return out


def _rate(history: dict, cid: str, mode: str) -> str:
    latest = next(
        (
            e
            for e in reversed(history[cid])
            if e.get("mode") == mode and e.get("thresholdMet") is not None
        ),
        None,
    )
    return f"{latest['passes']}/{latest['healthy']}" if latest else "?"


def _print_summary(history: dict, buckets: dict, mode: str, recorded: bool) -> None:
    for cid in buckets["regressed"]:
        print(f"  REGRESSED  {cid}  (now {_rate(history, cid, mode)})")
    for cid in buckets["recovered"]:
        print(f"  RECOVERED  {cid}  (now {_rate(history, cid, mode)})")
    for cid in buckets["flaky"]:
        print(f"  FLAKY      {cid}  (now {_rate(history, cid, mode)})")
    stable = buckets["stable"]
    if stable:
        print(f"  STABLE({len(stable)})  {', '.join(stable)}")
    if not any(buckets.values()):
        print("  no cross-run deltas")
    print(f"\nhistory {'updated' if recorded else 'NOT written'} ({mode} run).")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Roll eval report.json into cross-run trends."
    )
    p.add_argument("report", type=Path, help="report.json from run-evals.mjs --json")
    p.add_argument("--history", type=Path, default=HISTORY)
    p.add_argument("--mode", choices=["hermetic", "subscription"], default="hermetic")
    p.add_argument("--window", type=int, default=5)
    p.add_argument("--stable-n", type=int, default=3)
    p.add_argument(
        "--no-write", action="store_true", help="report only; don't update history"
    )
    args = p.parse_args(argv)

    report = json.loads(args.report.read_text())
    if args.mode != "hermetic":
        failing = [c["id"] for c in report if c.get("status") == "FAIL"]
        print(
            "subscription run — trends require hermetic (API-key) runs; not recorded."
        )
        print(f"  current FAILs: {', '.join(failing) if failing else 'none'}")
        return 0

    history = record_run(
        load_history(args.history), report, int(time.time()), args.mode
    )
    buckets = classify(
        history, mode=args.mode, window=args.window, stable_n=args.stable_n
    )
    recorded = not args.no_write
    _print_summary(history, buckets, args.mode, recorded)
    if recorded:
        write_json(args.history, history)
    return 1 if buckets["regressed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
