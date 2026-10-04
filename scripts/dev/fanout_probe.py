#!/usr/bin/env python3
"""The `fanout` source for cc-wait: a fan-out run's batches, read through `fanout_place status`.

`cc-wait fanout <run-id> [<run-id> ...]` runs this through `.claude/wait-sources/fanout`. It
replaces the Monitor loop each orchestrating session wrote by hand around `status`, 92 of them
in the two weeks to 2026-10-04 (`while true; do fanout_place.py status <id> | grep -v
': running$'; sleep 120; done`).

    fanout_probe.py --describe <run-id>...   # terminal states, as JSON
    fanout_probe.py <run-id>...              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md.

ONE DEFINITION OF A BATCH'S STATE. This runs `fanout_place.main(["status", <run-id>])` and
reads what it prints, so `status` stays the only code that decides a batch's state and its
exit tier (the issue-fanout skill documents both).

THE STATE. `running` while any batch runs or could not be read, with the finished batches as
its detail. A Monitor running `cc-wait fanout` therefore prints one line each time a batch
finishes, and nothing in between. Once no batch runs, the worst exit tier `status` returned
decides:

    finished         0   every batch landed, is done, or was cleaned
    needs-attention  1   a batch needs a hand: needs-input, no-pr, no-verdict, no-report
    failed           5   a batch failed (status's own tier 5)

The detail then carries each batch line that is not plain success, so the last line is what
to act on.

A REMOTE SOURCE. `status` reads each host over ssh and asks GitHub about merged PRs, so this
declares `remote` and a 90s interval. cc-wait then shares one read per interval among every
watcher of the same runs, and the reads stay inside `ufw limit ssh` (6 per 30s).
"""

import argparse
import contextlib
import io
import json
import re
import sys
from collections.abc import Callable

import fanout_place
from fanout_lib import manifest as manifest_mod

TERMINAL = {"finished": 0, "needs-attention": 1, "failed": 5}

_LINE = re.compile(r"^(?P<batch>\S+) on (?P<host>\S+): (?P<state>\S+)")
_TIMED_OUT = "status read timed out"
_SUCCESS = frozenset({"done", "landed", "cleaned"})

# (exit tier, printed lines) for one run id.
StatusReader = Callable[[str], tuple[int, str]]


def read_status(run_id: str) -> tuple[int, str]:
    """`fanout_place.py status <run_id>`'s exit tier and output, run in this process."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fanout_place.main(["status", run_id])
    return rc, out.getvalue()


def describe() -> dict:
    return {"terminal": TERMINAL, "interval_s": 90, "remote": True}


def read(run_ids: list[str], status: StatusReader = read_status) -> dict:
    """The runs' state: `running` until no batch runs, then the worst tier's state."""
    worst = 0
    running: list[str] = []
    finished: list[tuple[str, str, str]] = []
    for run_id in run_ids:
        rc, text = status(run_id)
        worst = max(worst, rc)
        for line in text.splitlines():
            match = _LINE.match(line)
            if not match:
                continue
            if match["state"] == "running" or line.endswith(_TIMED_OUT):
                running.append(match["batch"])
            else:
                finished.append((match["batch"], match["state"], line.strip()))
    finished.sort()
    if running:
        done = ", ".join(f"{batch} {state}" for batch, state, _ in finished) or "none"
        return {
            "state": "running",
            "detail": f"{len(running)} running; finished: {done}",
        }
    attention = [line for _batch, state, line in finished if state not in _SUCCESS]
    state = "failed" if worst >= 5 else "needs-attention" if worst >= 1 else "finished"
    detail = " | ".join(attention) or f"{len(finished)} batches finished"
    return {"state": state, "detail": detail[:600]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fanout_probe.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("run_ids", nargs="+", metavar="run-id")
    ns = parser.parse_args(argv)
    for run_id in ns.run_ids:
        try:
            manifest_mod.load(run_id)
        except (OSError, ValueError) as exc:
            print(f"fanout_probe: no fan-out run {run_id!r}: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(describe() if ns.describe else read(ns.run_ids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
