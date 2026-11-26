"""Run a `launch --review` fan-out batch: implement, review, fix, then land.

A batch's transient unit runs this in place of `claude -p`, with the batch's worktree as its
cwd, the brief on stdin and `.fanout/report.json` as stdout. `fanout_lib.review.review` holds the
phases and the reasons for them.

Usage::

    fanout.py review --batch 1345-1386 --repo DanielH2018/server < .fanout/brief.md
    fanout.py review --batch 1345-1386 --repo DanielH2018/server --resume-conflict < .fanout/brief.md

The second form is the unit `fanout.py place resume` starts for a batch that has ended: it
runs `Pipeline.run_conflict` in place of every phase.
"""

import json
import os
import stat
import sys
from typing import TextIO
from pathlib import Path


from dev.fanout_lib.launch import RESUME_FLAG, RESUME_REQUEST
from dev.fanout_lib.review.api import Pipeline
from dev.fanout_lib.sizing import DEFAULT, LADDER, by_name
from dev.fanout_lib.target import resolve
from dev.fanout_lib.transport import this_host
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK


def main(argv=None) -> int:
    """Run the batch's phases and write the last phase's report.

    Exits 1 when that report carries `is_error`, so the unit's own result agrees with the
    report `status` reads instead of recording a failed batch as a clean exit.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    report = run(build(argv, sys.stdin.read()), argv)
    write_report(report, sys.stdout, Path(".fanout") / "report.json")
    return exit_code(report)


def run(pipeline: Pipeline, argv: list[str]) -> dict:
    """Every phase, or with `RESUME_FLAG` only the conflict `place resume` named.

    `fanout.py place resume` wrote the PR URL and the landing's `VERDICT:` line to
    `RESUME_REQUEST` in the worktree before it started this unit.
    """
    if RESUME_FLAG not in argv:
        return pipeline.run_all()
    request = json.loads((pipeline.worktree / RESUME_REQUEST).read_text())
    return pipeline.run_conflict(str(request["pr"]), str(request["line"]))


def build(argv: list[str] | None, brief: str) -> Pipeline:
    """The pipeline `argv` describes, in the cwd's worktree, fed `brief`.

    It drops the launcher's `PYTHONPATH` from the environment the phases' children inherit.
    That root names this batch's tree, and a child in another checkout, such as land.sh's
    fresh one, would import from it ahead of its own `.pth`.
    """
    os.environ.pop("PYTHONPATH", None)
    p = ArgumentParser(
        prog="fanout.py review", description=__doc__.splitlines()[0], allow_abbrev=False
    )
    p.add_argument("--batch", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument(
        "--rung",
        choices=[rung.name for rung in LADDER],
        default=DEFAULT.name,
        help="the model and effort the implementer runs at",
    )
    p.add_argument(
        RESUME_FLAG,
        action="store_true",
        help="run only the merge-conflict resume, for the landing `place resume` names "
        f"in {RESUME_REQUEST}",
    )
    args = p.parse_args(argv)
    pipeline = Pipeline(Path.cwd(), args.batch, this_host(), resolve(args.repo), brief)
    pipeline.record.implementer = by_name(args.rung)
    return pipeline


def exit_code(report: dict) -> int:
    """1 for a report carrying `is_error`, which `status` reads as a failed batch; else 0."""
    return FAILED if report.get("is_error") else OK


def write_report(report: dict, out: TextIO, path: Path | None = None) -> None:
    """Write `report` as the whole of `out`, emptying it first when it is a regular file.

    systemd opens `.fanout/report.json` without truncating it, and status reads the last
    result line there, so a line an agent planted past this report's end would win.
    """
    if stat.S_ISREG(os.fstat(out.fileno()).st_mode):
        out.flush()
        os.ftruncate(out.fileno(), 0)
        out.seek(0)
    print(json.dumps(report), file=out)
    out.flush()
    # `status` reads the file by name: an agent that replaced it with another file or a symlink left
    # this process writing to an inode nobody reads.
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
