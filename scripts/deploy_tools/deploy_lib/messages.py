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
from lib.exit_messages import VERDICT_PREFIXES, verdict_line
from lib.exit_messages import describe as describe_exit

PROG = "deploy.sh"
ENTRY_POINT = "scripts/deploy.sh"
# The verdict of an exit 0 that deployed nothing, which `CONTRACTS`' one row per code cannot
# name: its exit-0 row reads "deployed".
NOTHING_TO_DEPLOY = "nothing-to-deploy"


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


def report(rc: int, argv: list[str], out=None, nothing: bool = False) -> None:
    """Print what `rc` means and the run's one-line verdict, both from `lib.exit_codes`.

    The script that exits is the one place that cannot go stale, so the meaning of each code
    is printed here rather than copied into hooks and docs.

    Args:
      rc: the code `run` returned.
      argv: this invocation's arguments, quoted in the verdict's detail so a log holding
        several runs says which one each verdict belongs to.
      out: where to write. Resolved at call time rather than bound as a default, so it is
        `sys.stderr` as replaced by whoever redirected it. stderr, so a caller parsing
        stdout for a tag list is unaffected.
      nothing: a gate ended the run with nothing to deploy (`flags.NothingToDeploy`). The
        verdict then reads `nothing-to-deploy`, because exit 0's row reads "deployed".

    A run that reached `--check`, `--dry-run` or `--list-services` never gets here: those
    exec ansible-playbook or `deploy_tags.py list` in place, and the exit code is that
    command's rather than one of these.
    """
    out = out if out is not None else sys.stderr
    # Only on a non-zero exit: a successful deploy has already printed a PLAY RECAP, and its
    # meaning is "it worked".
    note = describe_exit(ENTRY_POINT, rc) if rc else None
    if note:
        print(f"{PROG}: error: {note}", file=out)
    detail = " ".join(argv) or "no arguments"
    verdict = verdict_line(ENTRY_POINT, rc, detail)
    if nothing:
        verdict = f"{VERDICT_PREFIXES[ENTRY_POINT]}: {NOTHING_TO_DEPLOY} ({detail})"
    if verdict:
        print(verdict, file=out)
