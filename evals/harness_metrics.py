#!/usr/bin/env python3
"""Three measured numbers for the Claude Code harness itself, each with its evidence.

Run: uv run python evals/harness_metrics.py [--json]

(a) **Bash-classifier precision/recall** over the labelled corpus
    `.claude/hooks/tests/test_command_vectors.py` reads — the same fixture, loaded the same
    way, run through the same `classify()` in `.claude/hooks/auto-approve-readonly.py`. A
    "positive" is the classifier auto-approving a command; recall is over the read-only
    vectors (did it approve what it should), precision is over what it approved (did it
    approve only what it should).
(b) **Hook firing counts over the last 7 days**, read from `~/.claude/logs/*.jsonl` if any
    hook-tagged line falls in that window. The OTEL/Loki pipeline is explicitly out of
    scope. When nothing local carries a hook-tagged line in the window, this reports
    "no local source" for that number rather than a 0 — a 0 would read as "hooks fired zero
    times", which nothing here can tell apart from "no log was read".
(c) **The review false-positive rate** from `evals/review_outcomes.jsonl`, computed by
    `scripts/dev/review_metrics.py` and reused here rather than re-derived.
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / ".claude" / "hooks"
LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_FIXTURE = (
    Path.home() / ".local/share/chezmoi/tests/fixtures/command-vectors.json"
)

sys.path.insert(0, str(REPO / "scripts" / "dev"))
from review_metrics import build_table, load_outcomes  # noqa: E402


def load_classifier():
    """Load `auto-approve-readonly.py`'s `classify()` the way the paired hook test does.

    The module's dashed filename blocks a normal import, so this loads it by file path
    instead, mirroring `.claude/hooks/tests/test_command_vectors.py::_load_classifier`.
    """
    sys.path.insert(0, str(HOOKS_DIR))  # auto-approve-readonly.py imports _hook_common
    spec = importlib.util.spec_from_file_location(
        "auto_approve_readonly", HOOKS_DIR / "auto-approve-readonly.py"
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


def load_vectors(fixture: Path = DEFAULT_FIXTURE) -> list[dict]:
    if not fixture.is_file():
        return []
    return json.loads(fixture.read_text(encoding="utf-8"))["vectors"]


def precision_recall(vectors: list[dict], classify) -> dict:
    """Precision/recall of `classify` against the corpus's `readonly` label."""
    tp = fp = fn = tn = 0
    for v in vectors:
        approved = classify(v["command"]) is not None
        if v["readonly"] and approved:
            tp += 1
        elif v["readonly"] and not approved:
            fn += 1
        elif not v["readonly"] and approved:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "n": len(vectors),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
    }


def classifier_metrics(fixture: Path = DEFAULT_FIXTURE) -> dict:
    vectors = load_vectors(fixture)
    if not vectors:
        return {
            "n": 0,
            "precision": None,
            "recall": None,
            "source": str(fixture),
            "note": "corpus not present locally",
        }
    result = precision_recall(vectors, load_classifier())
    result["source"] = str(fixture)
    return result


def hook_firing_counts(logs_dir: Path = LOGS_DIR, days: int = 7) -> dict:
    """Count log lines carrying a `hook` field within the last `days`, by hook name.

    Scans every `*.jsonl` under `logs_dir`. A line missing `ts` or `hook`, or one that
    fails to parse, is skipped rather than counted.

    Returns:
        `{"source": "no local source"}` when nothing local falls in the window — the
        signal that this measured nothing, as opposed to a real 0.
    """
    if not logs_dir.is_dir():
        return {"source": "no local source"}
    cutoff = time.time() - days * 86400
    counts: dict[str, int] = {}
    files_read = []
    for f in sorted(logs_dir.glob("*.jsonl")):
        matched_any = False
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            hook, ts = obj.get("hook"), obj.get("ts")
            if not hook or not ts:
                continue
            try:
                epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if epoch < cutoff:
                continue
            counts[hook] = counts.get(hook, 0) + 1
            matched_any = True
        if matched_any:
            files_read.append(str(f))
    if not counts:
        return {"source": "no local source"}
    return {"source": files_read, "days": days, "counts": counts}


def review_false_positive_rate() -> dict:
    table = build_table(load_outcomes())
    rated = [r for r in table if r["false_positive_rate"] is not None]
    if not rated:
        return {
            "source": "evals/review_outcomes.jsonl",
            "rate": None,
            "runs_with_data": 0,
        }
    latest = rated[-1]
    return {
        "source": "evals/review_outcomes.jsonl",
        "rate": latest["false_positive_rate"],
        "date": latest["date"],
        "runs_with_data": len(rated),
    }


def collect() -> dict:
    return {
        "classifier": classifier_metrics(),
        "hook_firing_7d": hook_firing_counts(),
        "review_false_positive_rate": review_false_positive_rate(),
    }


def format_report(data: dict) -> str:
    lines = []
    c = data["classifier"]
    if c["n"]:
        lines.append(
            f"classifier: precision={c['precision']:.3f} recall={c['recall']:.3f} "
            f"(n={c['n']}, tp={c['tp']} fp={c['fp']} fn={c['fn']} tn={c['tn']}) "
            f"source={c['source']}"
        )
    else:
        lines.append(f"classifier: no corpus at {c['source']}")
    h = data["hook_firing_7d"]
    if h["source"] == "no local source":
        lines.append("hook firing (7d): no local source")
    else:
        total = sum(h["counts"].values())
        lines.append(
            f"hook firing (7d): {total} events across {len(h['counts'])} hooks, "
            f"from {h['source']}"
        )
        for name, n in sorted(h["counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {n}")
    r = data["review_false_positive_rate"]
    if r["rate"] is None:
        lines.append("review false-positive rate: no runs with complete data")
    else:
        lines.append(
            f"review false-positive rate: {r['rate']:.3f} "
            f"(latest complete run: {r['date']}, {r['runs_with_data']} runs with data)"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON instead of text"
    )
    args = parser.parse_args()
    data = collect()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(format_report(data))


if __name__ == "__main__":
    main()
