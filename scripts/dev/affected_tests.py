#!/usr/bin/env python3
"""Record what each test module depends on, and say which modules a diff can affect.

A pull request's CI run executes only the test modules its diff can affect. The table
that decides this is `affected_tests.json` beside this file. `record` writes it and
`pytest_shard.py --changed-since` reads it on each CI leg. `select` prints the same answer
for a diff you name.

`record` runs each module alone under `strace`, so the table holds what the kernel saw the
module's process tree read: `affected_lib/record.py` has how, and `affected_lib/table.py` has
the selection rule and what a row can miss. It needs `strace` on the host, and it runs test
modules, so it takes minutes: 122 modules took 265s at six jobs on daniel-server, about 54
minutes for all of them. The docs-refresh cron therefore runs `record --slices 12 --budget
540` on every tick. That records each module with no current row first, then one twelfth of
the rest, chosen by the 12-hour period, so every row is recorded again within six days.

Usage:
    uv run python scripts/dev/affected_tests.py record --slices 12 --budget 540  # the cron's
    uv run python scripts/dev/affected_tests.py record                     # every module
    uv run python scripts/dev/affected_tests.py record tests/repo/test_x.py  # these rows only
    uv run python scripts/dev/affected_tests.py select --base origin/master
    uv run python scripts/dev/affected_tests.py select --base HEAD^1 --list
"""

import hashlib
import random
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

# Reach `scripts/lib`: a directly-invoked script gets only its own directory on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest_shard
from affected_lib import record, table
from lib.cli_help import ArgumentParser
from lib.repo_paths import REPO

# The docs-refresh cron runs at 06:17 and 18:17. Two runs 12 hours apart always fall in
# consecutive periods of this length, so successive runs visit successive slices.
_SLICE_PERIOD_SECONDS = 12 * 60 * 60


def _slice(module: str, slices: int) -> int:
    """The 0-based slice of `slices` a module is recorded again in, by a hash of its path."""
    return int(hashlib.sha256(module.encode()).hexdigest(), 16) % slices


def due(
    rows: table.Table,
    census: list[str],
    blobs: dict[str, str],
    slices: int,
    seconds: float,
) -> list[str]:
    """The modules a run at epoch `seconds` records, most urgent first.

    The modules with no current row come first, in path order. One of `slices` hash slices of
    the rest follows, shuffled so that a budget which ends the run early does not leave the
    same modules unrecorded on every visit.
    """
    stale = sorted(m for m in census if not rows.current(m, blobs))
    index = int(seconds // _SLICE_PERIOD_SECONDS) % slices
    visit = sorted(
        m for m in census if rows.current(m, blobs) and _slice(m, slices) == index
    )
    random.Random(int(seconds)).shuffle(visit)
    return stale + visit


def _record(args) -> int:
    if shutil.which("strace") is None:
        print(
            "affected_tests: error: strace is not installed, so no module can be traced",
            file=sys.stderr,
        )
        return 1
    census = pytest_shard.census()
    if unknown := sorted(set(args.modules) - set(census)):
        print(
            "affected_tests: error: not a committed test file under testpaths: "
            + ", ".join(unknown),
            file=sys.stderr,
        )
        return 1
    rows = table.load()
    for gone in set(rows.rows) - set(census):
        rows.drop(gone)
    queue = args.modules or due(
        rows, census, table.tracked_blobs(REPO), args.slices, time.time()
    )
    start = time.monotonic()
    recorded, refused = record.refresh(
        rows, queue, REPO, budget_seconds=args.budget, jobs=args.jobs
    )
    table.dump(rows)
    print(
        f"recorded {len(recorded)} of {len(queue)} due row(s) in "
        f"{time.monotonic() - start:.0f}s; {len(rows.rows)} of {len(census)} modules have a row"
    )
    for reason, count in sorted(Counter(refused.values()).items()):
        names = sorted(m for m, r in refused.items() if r == reason)
        print(f"  no row, {reason} ({count}): {', '.join(names)}")
    return 0


def _select(args) -> int:
    selection = pytest_shard.affected(args.base)
    scope = "the whole census" if selection.full else "selected"
    print(f"{len(selection.modules)} module(s), {scope}: {selection.reason}")
    if args.list:
        print("\n".join(selection.modules))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="trace test modules and rewrite their rows")
    rec.add_argument(
        "modules", nargs="*", metavar="MODULE", help="record only these modules"
    )
    rec.add_argument(
        "--budget",
        type=float,
        metavar="SECONDS",
        help="start no module after this long; the rest keep their rows for the next run",
    )
    rec.add_argument(
        "--jobs", type=int, default=4, help="modules traced at once (default 4)"
    )
    rec.add_argument(
        "--slices",
        type=int,
        default=1,
        metavar="N",
        help="after the modules with no current row, record one of N slices of the rest "
        "(default 1, which is all of them)",
    )
    rec.set_defaults(run=_record)
    sel = sub.add_parser(
        "select", help="print what the diff from a base to HEAD selects"
    )
    sel.add_argument(
        "--base", required=True, metavar="REF", help="the commit to diff from"
    )
    sel.add_argument(
        "--list", action="store_true", help="also print each selected module"
    )
    sel.set_defaults(run=_select)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
