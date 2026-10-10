#!/usr/bin/env python3
"""The `fanout` source for cc-wait: a fan-out run's batches, read through `fanout_place status`.

`cc-wait fanout <run-id> [<run-id> ...]` runs this through `.claude/wait-sources/fanout`. It
replaces the Monitor loop each orchestrating session wrote by hand around `status`, 92 of them
in the two weeks to 2026-10-04 (`while true; do fanout_place.py status <id> | grep -v
': running$'; sleep 120; done`).

    fanout_probe.py --describe <run-id>...   # terminal states, as JSON
    fanout_probe.py <run-id>...              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md.

ONE DEFINITION OF A BATCH'S STATE. This runs `fanout_place.main(["status", <run-id>,
"--json"])` and reads the `state` field of each row it prints, so `status` stays the only code
that decides a batch's state and its exit tier (the issue-fanout skill documents both). It
read the text lines with a regex until #3926, so a change to their wording broke it silently.

THE STATE. `running` while any batch runs or could not be read, with the finished batches as
its detail. A Monitor running `cc-wait fanout` therefore prints one line each time a batch
finishes, and nothing in between. Once no batch runs, the worst exit tier `status` returned
decides:

    finished         0   every batch landed, is done, or was cleaned
    needs-attention  1   a batch needs a hand: needs-input, no-pr, no-verdict, no-report
    failed           5   a batch failed (status's own tier 5)

The detail then carries each batch line that is not plain success, so the last line is what
to act on. An output with no batch line this can parse is unreadable, never `finished`: the
probe exits 1, so cc-wait counts a failed read.

A REMOTE SOURCE. `status` reads each host over ssh and asks GitHub about merged PRs, so this
declares `remote` and a 90s interval. cc-wait then shares one read per interval among every
watcher of the same runs, and the reads stay inside `ufw limit ssh` (6 per 30s).
"""

import argparse
import contextlib
import io
import json
import sys
from collections.abc import Callable

import fanout_place
from fanout_lib import manifest as manifest_mod
from fanout_lib.status import UNREAD

TERMINAL = {"finished": 0, "needs-attention": 1, "failed": 5}

_SUCCESS = frozenset({"done", "landed", "cleaned"})
# A batch still working, or one whose host read timed out: neither has finished.
_OPEN = frozenset({"running", UNREAD})

# (exit tier, the rows `status --json` printed) for one run id.
StatusReader = Callable[[str], tuple[int, list[dict]]]


class Unreadable(Exception):
    """`status` printed no batch row this probe can read."""


def read_status(
    run_id: str, main: Callable[[list[str]], int] | None = None
) -> tuple[int, list[dict]]:
    """`fanout_place.py status <run_id> --json`'s exit tier and rows, run in this process.

    Args:
        run_id: the run to read.
        main: `fanout_place.main`-shaped; a seam so a test can point it at its own
            manifest root and fake tools.

    Raises:
        Unreadable: the output is not a JSON array.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = (main or fanout_place.main)(["status", run_id, "--json"])
    try:
        rows = json.loads(out.getvalue())
    except ValueError as exc:
        raise Unreadable(f"`status {run_id} --json` printed no JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise Unreadable(f"`status {run_id} --json` printed no array")
    return rc, rows


def describe() -> dict:
    return {"terminal": TERMINAL, "interval_s": 90, "remote": True}


def read(run_ids: list[str], status: StatusReader = read_status) -> dict:
    """The runs' state: `running` until no batch runs, then the worst tier's state."""
    worst = 0
    running: list[str] = []
    finished: list[tuple[str, str, str]] = []
    for run_id in run_ids:
        rc, rows = status(run_id)
        worst = max(worst, rc)
        for row in rows:
            if row["state"] in _OPEN:
                running.append(row["batch"])
            else:
                finished.append((row["batch"], row["state"], row["line"]))
    finished.sort()
    if not running and not finished:
        # No row is an unreadable output, not a finished run: the worst tier of an empty
        # read is 0, which would end the wait as `finished`. Unreadable is a failed read,
        # which cc-wait retries and then gives up on with exit 2.
        raise Unreadable(f"no batch row in `status` output for {', '.join(run_ids)}")
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
    try:
        print(json.dumps(describe() if ns.describe else read(ns.run_ids)))
    except Unreadable as exc:
        print(f"fanout_probe: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
