"""The `fanout` source for cc-wait: a fan-out run's batches, read through `fanout.py place status`.

`cc-wait fanout <run-id> [<run-id> ...]` runs this through `.claude/wait-sources/fanout`. It
replaces the Monitor loop each orchestrating session wrote by hand around `status`, 92 of them
in the two weeks to 2026-10-04 (`while true; do fanout.py place status <id> | grep -v
': running$'; sleep 120; done`).

    fanout.py probe --describe <run-id>...   # terminal states, as JSON
    fanout.py probe <run-id>...              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md.

ONE DEFINITION OF A BATCH'S STATE. This runs `fanout_lib.place.main(["status", <run-id>,
"--json"])` and reads the `state` field of each row it prints, so `status` stays the only code
that decides a batch's state and its exit tier (the issue-fanout skill documents both). A
regex over the text lines would break silently when their wording changes.

THE STATE. `running` while any batch runs, could not be read, or is `done` with its PR still
the server lander's to land (the row's `awaiting_lander` field), with a count of the finished
batches as its detail, and the name of each one that needs a hand. `status` ends an awaiting
batch as `landed` or as `needs-input` (`status.AWAIT_LANDER_S` bounds a wait the lander
never ends), so this wait ends with it. A Monitor running `cc-wait
fanout` therefore prints one line each time a batch finishes, and nothing in between. Once no batch runs, the worst exit tier `status` returned
decides:

    finished         0   every batch landed, is done, or was cleaned
    needs-attention  1   a batch needs a hand: parked, needs-input, no-pr, no-verdict,
                         no-report
    failed           5   a batch failed (status's own tier 5)

The detail then carries each batch line that is not plain success, so the last line is what
to act on. An output with no batch line this can parse is unreadable, never `finished`: the
probe exits 1, so cc-wait counts a failed read.

A REMOTE SOURCE. `status` reads each host over ssh and asks GitHub about merged PRs, so this
declares `remote` and a 90s interval. cc-wait then shares one read per interval among every
watcher of the same runs, and the reads stay inside `ufw limit ssh` (6 per 30s).
"""

import contextlib
import io
import json
import sys
from collections.abc import Callable


from dev.fanout_lib import manifest as manifest_mod
from dev.fanout_lib import place
from dev.fanout_lib.status import UNREAD, exit_tier
from lib.cli_help import ArgumentParser, refuse_help_under_describe
from lib.exit_codes import FANOUT_PLACE_BATCH_FAILED, FANOUT_PLACE_OK

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
    """`fanout.py place status <run_id> --json`'s exit tier and rows, run in this process.

    Args:
        run_id: the run to read.
        main: `fanout_lib.place.main`-shaped; a seam so a test can point it at its own
            manifest root and fake tools.

    Raises:
        Unreadable: the output is not a JSON array.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = (main or place.main)(["status", run_id, "--json"])
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
    """The runs' state: `running` until no batch runs or awaits the lander, then the worst tier's."""
    tiers: list[int] = []
    running: list[str] = []
    awaiting: list[str] = []
    finished: list[tuple[str, str, str]] = []
    for run_id in run_ids:
        rc, rows = status(run_id)
        tiers.append(rc)
        for row in rows:
            if row["state"] in _OPEN:
                running.append(row["batch"])
            elif row.get("awaiting_lander"):
                awaiting.append(row["batch"])
            else:
                finished.append((row["batch"], row["state"], row["line"]))
    finished.sort()
    if not running and not awaiting and not finished:
        # No row is an unreadable output, not a finished run: the worst tier of an empty
        # read is 0, which would end the wait as `finished`. Unreadable is a failed read,
        # which cc-wait retries and then gives up on with exit 2.
        raise Unreadable(f"no batch row in `status` output for {', '.join(run_ids)}")
    if running or awaiting:
        # Counts, not names: cc-wait prints a line whenever the detail changes, so a finish still
        # prints once. A batch needing a hand keeps its name.
        stuck = [
            f"{batch} {state}" for batch, state, _ in finished if state not in _SUCCESS
        ]
        detail = f"{len(running)} running; {len(finished)} finished"
        if awaiting:
            detail += f"; {len(awaiting)} awaiting the server lander"
        if stuck:
            detail += f" ({', '.join(stuck)})"
        return {"state": "running", "detail": detail}
    attention = [line for _batch, state, line in finished if state not in _SUCCESS]
    # By name, not `>=`: `status` exits 75 for a timed-out read, larger than 5.
    worst = exit_tier(tiers)
    state = (
        "failed"
        if worst == FANOUT_PLACE_BATCH_FAILED
        else "finished"
        if worst == FANOUT_PLACE_OK
        else "needs-attention"
    )
    detail = " | ".join(attention) or f"{len(finished)} batches finished"
    return {"state": state, "detail": detail[:600]}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="fanout.py probe", description=(__doc__ or "").partition("\n")[0]
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("run_ids", nargs="+", metavar="run-id")
    refuse_help_under_describe(parser, argv)
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
