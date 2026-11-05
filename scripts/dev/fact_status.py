#!/usr/bin/env python3
"""Lint, report and diff notice for the fact stores — the one entry point.

``lint`` reports citations that cannot be support. ``report`` reads git and the instructions
log: the sections citing nothing ranked by how often their doc loads, the instruction bytes
each session loaded, and the notice replayed over the window (``--json`` for a cron or an
agent). ``notice`` lists every section citing an atom that the diff from the merge base of
``--since`` to ``--head``, or to the working tree, moved.
It exits 0 with rows or without, and 2 only when git cannot resolve the merge base.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import json
from pathlib import Path

from lib.cli_help import ArgumentParser

from lib.exit_codes import FACT_STATUS_BAD_ARGS, FACT_STATUS_BAD_REF

from lib.facts.citations import repo_citations
from lib.facts.lint import changed_units, lint_sections
from lib.facts.notice import FORMATS, resolve_base, scan
from lib.facts.notice import render as render_notice
from lib.facts.report import build, default_log, render
from lib.repo_paths import REPO

_USAGE = FACT_STATUS_BAD_ARGS

# The next step each command prints after its findings, so a red run ends on what to do (#4448).
FIX_LINT = (
    "fix: .claude/rules/facts.md describes each rule above and how to resolve it."
)


def cmd_lint(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    try:
        keys = changed_units(repo, args.changed_since) if args.changed_since else None
    except ValueError as unresolvable:
        print(unresolvable, file=_sys.stderr)
        return _USAGE
    findings = lint_sections(repo, keys, since=args.changed_since)
    for f in findings:
        print(f"{'warn ' if f.warn else 'ERROR'} {f.rule:<20} {f.unit}: {f.detail}")
    if findings:
        print(FIX_LINT)
    return 1 if any(not f.warn for f in findings) else 0


def cmd_report(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    log = Path(args.log) if args.log else default_log(repo)
    report = build(repo, repo_citations(repo), args.days, log)
    print(json.dumps(report, indent=2) if args.json else render(report, args.top))
    return 0


def cmd_notice(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    base = resolve_base(repo, args.since, args.head or "HEAD")
    if base is None:
        print(
            f"no merge base of {args.since!r} and {args.head or 'HEAD'!r}; "
            "is the ref fetched?",
            file=_sys.stderr,
        )
        return FACT_STATUS_BAD_REF
    rows, summary = scan(repo, base, args.head)
    print(render_notice(rows, summary, args.format))
    return 0


def _add_common(sp: argparse.ArgumentParser) -> None:
    """Add ``--repo``/``--store`` to a subparser.

    On the top-level parser alone these must precede the subcommand token
    (``fact_status.py --repo P lint``); every caller here writes the flag
    after the subcommand instead (``lint --repo P``), which argparse only
    accepts when each subparser carries its own copy.
    """
    sp.add_argument("--repo", default=str(REPO))
    sp.add_argument("--store", choices=["repo"], default="repo")


def main(argv: list[str] | None = None) -> int:
    p = ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    ln = sub.add_parser("lint")
    _add_common(ln)
    ln.add_argument("--changed-since", default=None)
    ln.set_defaults(fn=cmd_lint)
    rp = sub.add_parser("report")
    _add_common(rp)
    rp.add_argument("--days", type=int, default=30, help="the window, in days")
    rp.add_argument(
        "--log",
        default=None,
        help="the instructions log (default: the primary checkout's)",
    )
    rp.add_argument(
        "--top", type=int, default=15, help="sections to print in text form"
    )
    rp.add_argument("--json", action="store_true", help="print one JSON object")
    rp.set_defaults(fn=cmd_report)
    nt = sub.add_parser("notice")
    _add_common(nt)
    nt.add_argument(
        "--since",
        default="origin/master",
        help="diff from the merge base of this ref and the head",
    )
    nt.add_argument(
        "--head",
        default=None,
        help="the revision to read as the head (default: the working tree)",
    )
    nt.add_argument("--format", choices=FORMATS, default="text")
    nt.set_defaults(fn=cmd_notice)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
