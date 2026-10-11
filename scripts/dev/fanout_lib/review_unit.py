"""Run a `launch --review` fan-out batch: implement, review, fix, then land.

A batch's transient unit runs this in place of `claude -p`, with the batch's worktree as its
cwd, the brief on stdin and `.fanout/report.json` as stdout. `fanout_lib.review.review` holds the
phases and the reasons for them.

Usage::

    fanout.py review --batch 1345-1386 --repo DanielH2018/server [--red-green] < .fanout/brief.md
"""

import argparse
import json
import os
import stat
import sys
from typing import TextIO
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.review.api import Pipeline
from fanout_lib.target import resolve
from fanout_lib.transport import this_host


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="fanout.py review", description=__doc__.splitlines()[0], allow_abbrev=False
    )
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
        this_host(),
        resolve(args.repo),
        sys.stdin.read(),
        red_green=args.red_green,
    )
    write_report(pipeline.run_all(), sys.stdout, Path(".fanout") / "report.json")
    return 0


def write_report(report: dict, out: TextIO, path: Path | None = None) -> None:
    """Write `report` as the whole of `out`, emptying it first when it is a regular file.

    systemd opens `.fanout/report.json` without truncating it, and status reads the last
    result line there, so a line the red author planted past this report's end would win.
    """
    if stat.S_ISREG(os.fstat(out.fileno()).st_mode):
        out.flush()
        os.ftruncate(out.fileno(), 0)
        out.seek(0)
    print(json.dumps(report), file=out)
    out.flush()
    # `status` reads the file by name. A red author who deleted it and wrote another in its
    # place, or a symlink, left this process writing to an inode nobody reads (#3884).
    if path is not None and not _same_file(path, out):
        path.unlink(missing_ok=True)
        path.write_text(json.dumps(report) + "\n")


def _same_file(path: Path, out: TextIO) -> bool:
    try:
        named = path.lstat()
    except OSError:
        return False
    held = os.fstat(out.fileno())
    return stat.S_ISREG(named.st_mode) and (named.st_dev, named.st_ino) == (
        held.st_dev,
        held.st_ino,
    )
