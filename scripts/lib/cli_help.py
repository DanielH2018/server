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

Typical usage example:

    from lib.cli_help import answer_help

    def main(argv=None):
        answer_help(__doc__, argv)
        ...
"""

import sys

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
