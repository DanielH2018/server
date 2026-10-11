"""The `--help` contract every entry point under `scripts/` answers.

ONE CONVENTION: `-h` or `--help` prints what the script is for and exits 0, from any
environment. `scripts/lib/tests/test_entry_points_answer_help.py` runs every catalogued entry
point that way, so an operator can ask what a script does without reading it and without a
kubeconfig, a cluster, a lock or a network in reach.

WHY A HELPER AND NOT JUST argparse. argparse already gives this to the 60-odd entry points that
parse arguments, and those need nothing from here. This module is for the rest: a script that
takes no arguments, one whose arguments are positional and hand-checked, and — the case that
made the test fail rather than merely go unanswered — one whose real work starts *before* the
parse. `deploy.sh` ran its staleness check first and answered `--help` with exit 4;
`export_grafana_dashboards.py` resolved a kubeconfig at import and died; two scripts hung
instead of answering. `answer_help` is a guard to put at the very top of `main`, ahead of every
such step.

The shell scripts do the same thing with four lines of `case` rather than by sourcing this —
`land.sh` and `deploy.sh` are `exec` shims whose whole body is two statements, and a `source`
in front of that is more machinery than the check is worth.

THE ONE EXCEPTION is a cc-wait repo probe asked for `--describe` (`.claude/wait-sources/`).
cc-wait runs `<probe> --describe <args>` and parses its stdout as JSON, and `<args>` is
whatever followed the source name, so `cc-wait land --help` runs `land --describe --help`.
Help on stdout with exit 0 reached cc-wait as a malformed description (#4376).
`refuse_help_under_describe` makes that a usage error instead, which cc-wait prints. A probe
asked for `--help` alone still answers with exit 0.

A USAGE ERROR EXITS 64. argparse's own `error()` exits 2, which many entry points here also
return as an outcome (`CI_DISARMED`, `PUBLISH_PUSHED_NO_PR`, `DEPLOY_TAG_MISS`), so a mistyped
flag read as a real result. `ArgumentParser` below is argparse's with `error()` exiting
`lib.exit_codes.USAGE_ERROR`. Subparsers inherit it, because `add_subparsers` defaults its
`parser_class` to the parent's class. `scripts/lib/tests/test_exit_codes.py`'s census refuses a
bare `argparse.ArgumentParser(` in an entry point.

MESSAGES carry argparse's prefix. `warn` and `error` print `<prog>: warning: ...` and
`<prog>: error: ...` on stderr, the shape `parser.error` already gives a bad argument, so one
grep pattern finds every complaint a script makes and the line names the script it came from.
A helper module in `lib/` uses them too: it has no name of its own worth printing, and the
running script's name is the one an operator can act on (#4447).

Typical usage example:

    from lib.cli_help import ArgumentParser, answer_help

    def main(argv=None):
        answer_help(__doc__, argv)
        ...
"""

import argparse
import os
import sys
from pathlib import Path as _Path
from typing import NoReturn

# Reach `lib` from a caller that put only its own directory on sys.path.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.exit_codes import USAGE_ERROR

HELP_FLAGS = ("-h", "--help")


def wants_help(argv: list[str] | None = None) -> bool:
    """Whether `argv` (defaulting to the real one) asks for help."""
    argv = sys.argv[1:] if argv is None else argv
    return any(arg in HELP_FLAGS for arg in argv)


def answer_help(doc: str | None, argv: list[str] | None = None) -> None:
    """Print `doc` and exit 0 when `argv` asks for help; return otherwise.

    Raises SystemExit(0) on a help request, which is what makes it safe to call as the first
    statement of `main` — nothing after it runs.
    """
    if wants_help(argv):
        print((doc or "").strip())
        raise SystemExit(0)


class ArgumentParser(argparse.ArgumentParser):
    """argparse's parser, except that a usage error exits `USAGE_ERROR` (64) rather than 2."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(USAGE_ERROR, f"{self.prog}: error: {message}\n")


def refuse_help_under_describe(
    parser: argparse.ArgumentParser, argv: list[str] | None = None
) -> None:
    """Exit 64 with `parser`'s usage on stderr when `argv` asks for help beside `--describe`.

    A cc-wait probe calls this ahead of `parser.parse_args`. cc-wait keeps only the last 400
    characters of a refused probe's stderr, so this prints the usage line, not the full help.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--describe" in argv and wants_help(argv):
        parser.print_usage(sys.stderr)
        parser.exit(
            USAGE_ERROR,
            f"{parser.prog}: error: --describe answers cc-wait in JSON, so it takes no "
            "--help; run without --describe\n",
        )


def prog() -> str:
    """The running script's name, derived as argparse derives its default `prog`.

    Read at call time, so a test that sets `sys.argv` sees its own value.
    """
    return os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python"


def warn(message: str) -> None:
    """Print `<prog>: warning: <message>` on stderr."""
    print(f"{prog()}: warning: {message}", file=sys.stderr)


def error(message: str) -> None:
    """Print `<prog>: error: <message>` on stderr, and return.

    Unlike `parser.error`, this does not exit: the caller returns its own exit code, which is
    how a script keeps distinct codes for distinct failures.
    """
    print(f"{prog()}: error: {message}", file=sys.stderr)
