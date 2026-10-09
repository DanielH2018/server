"""Run a `launch --review` fan-out batch: implement, review, fix, then land.

A batch's transient unit runs this in place of `claude -p`, with the batch's worktree as its
cwd, the brief on stdin and `.fanout/report.json` as stdout. `fanout_lib.review` holds the
phases and the reasons for them.

Usage::

    fanout_review.py --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
"""

import argparse
import json
import sys
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
    print(json.dumps(pipeline.run_all()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
