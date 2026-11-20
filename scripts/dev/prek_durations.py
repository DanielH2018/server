#!/usr/bin/env python3
"""Print the slowest prek hooks of one run as a Markdown table, read from prek's trace log.

Run: uv run python scripts/dev/prek_durations.py <trace log> [--top N]

CI's `hooks` job runs `prek run --all-files --log-file <path>` without `--verbose`, so the job
log holds only a failing hook's output, and appends this script's table to the job summary.
prek closes one `run{hook_id=<id> ...}` span per hook run and logs its `time.busy` and
`time.idle`; their sum is the duration `--verbose` prints. Spans that share a hook id are
summed.

Exit codes: 0 the table printed, even an empty one; 1 the log is unreadable.
"""

import re
import sys


from pathlib import Path

from lib.cli_help import ArgumentParser

SPAN = re.compile(
    r"run\{hook_id=(?P<hook>\S+)[^}]*\}: close"
    r" time\.busy=(?P<busy>[\d.]+)(?P<busy_unit>ns|µs|us|ms|s)"
    r" time\.idle=(?P<idle>[\d.]+)(?P<idle_unit>ns|µs|us|ms|s)"
)
SECONDS_PER = {"ns": 1e-9, "µs": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1.0}


def durations(log: str) -> dict[str, float]:
    """Each hook's total run time in seconds, by hook id, from a prek trace log."""
    found: dict[str, float] = {}
    for match in SPAN.finditer(log):
        seconds = float(match["busy"]) * SECONDS_PER[match["busy_unit"]]
        seconds += float(match["idle"]) * SECONDS_PER[match["idle_unit"]]
        found[match["hook"]] = found.get(match["hook"], 0.0) + seconds
    return found


def table(found: dict[str, float], top: int) -> str:
    """A count line, then the `top` slowest hooks as a Markdown table, slowest first."""
    slowest = sorted(found.items(), key=lambda item: -item[1])[:top]
    lines = [
        f"{len(found)} hooks ran for {sum(found.values()):.1f}s in all;"
        f" the {len(slowest)} slowest:",
        "",
        "| hook | seconds |",
        "| --- | ---: |",
    ]
    lines += [f"| `{hook}` | {seconds:.1f} |" for hook, seconds in slowest]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path, help="the file prek's --log-file wrote")
    parser.add_argument(
        "--top", type=int, default=15, help="how many hooks to list (default 15)"
    )
    args = parser.parse_args(argv)
    try:
        log = args.log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"prek_durations: cannot read {args.log}: {exc}", file=sys.stderr)
        return 1
    print(table(durations(log), args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
