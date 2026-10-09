"""Run a `launch --review` fan-out batch: implement, review, fix, then land.

A batch's transient unit runs this in place of `claude -p`, with the batch's worktree as its
cwd, the brief on stdin and `.fanout/report.json` as stdout. `fanout_lib.review` holds the
phases and the reasons for them.

Usage::

    fanout_review.py --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
"""

import argparse
import json
import os
import stat
import sys
from typing import TextIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.review import Pipeline
from fanout_lib.target import resolve
from fanout_lib.transport import _local_host


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    p.add_argument("--batch", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument(
        "--red-green",
        action="store_true",
        help="write and gate failing tests before the implementer runs",
    )
    args = p.parse_args(argv)
    pipeline = Pipeline(
        Path.cwd(),
        args.batch,
        _local_host(),
        resolve(args.repo),
        sys.stdin.read(),
        red_green=args.red_green,
    )
    write_report(pipeline.run_all(), sys.stdout)
    return 0


def write_report(report: dict, out: TextIO) -> None:
    """Write `report` as the whole of `out`, emptying it first when it is a regular file.

    systemd opens `.fanout/report.json` without truncating it, and status reads the last
    result line there, so a line the red author planted past this report's end would win.
    """
    if stat.S_ISREG(os.fstat(out.fileno()).st_mode):
        out.flush()
        os.ftruncate(out.fileno(), 0)
        out.seek(0)
    print(json.dumps(report), file=out)


if __name__ == "__main__":
    sys.exit(main())
