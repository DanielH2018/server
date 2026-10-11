"""The prefix every line `deploy.sh` prints carries, and the three ways it prints one.

A complaint opens `deploy.sh: error:` or `deploy.sh: warning:`, the shape `lib.cli_help`
gives every script, so one grep finds what the deploy refused or doubted (#4478). A status
line, such as a lock wait the landing books, opens `deploy.sh:` with no severity word.

`PROG` is a literal rather than `cli_help.prog()`: `deploy.sh` execs `deploy_cli.py`, so the
derived name is the file the operator never typed, and the landing's regexes need a fixed
string to match.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_messages.py
"""

import sys
from pathlib import Path

# Reach `lib.` from scripts/: an importer that did not bootstrap it itself (a test, a REPL)
# finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.cli_help import error, warn

PROG = "deploy.sh"


def say(*lines: str) -> None:
    """Print status or continuation lines on stderr as given, flushed past ansible's output."""
    for line in lines:
        print(line, file=sys.stderr, flush=True)


def say_error(first: str, *rest: str) -> None:
    """`deploy.sh: error: <first>`, then each indented continuation line as given."""
    error(first, prog=PROG)
    say(*rest)


def say_warning(first: str, *rest: str) -> None:
    """`deploy.sh: warning: <first>`, then each indented continuation line as given."""
    warn(first, prog=PROG)
    say(*rest)
