#!/usr/bin/env python3
"""List the check files under `tests/` that cite an incident and have not failed on master lately.

A check file cites an incident when its text names an issue (`#1234`) or a date
(`2026-11-18`). The report reads master's red attempts over the window from the CI failure
survey (`ci_failures.py`) and prints each citing file that none of them reported failing,
with what it cites. A red attempt on a PR branch does not count. The report proposes
candidates and deletes nothing: a guard that never fails can still be load-bearing, so the
operator decides which go.

Usage:
    uv run python scripts/dev/quiet_checks.py --days 60

It needs `gh` and the sandbox off, like `ci_failures.py`. GitHub keeps an Actions log for 90
days by default, so a longer window reads most red attempts as unreadable; the report counts
those.

Exit codes: 0 the report printed; 1 a `gh` call that lists runs, attempts or jobs failed, and
stderr carries the last line of `gh`'s stderr; 64 a bad flag.
"""

import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import ci_failures

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.repo_paths import REPO

DEFAULT_DAYS = 60
BRANCH = "master"
CITATION = re.compile(r"(?<![\w/&])#\d{2,}\b|\b20\d\d-\d\d-\d\d\b")


def citing_checks(repo: Path = REPO) -> dict[str, list[str]]:
    """Map each check file under `tests/` that cites an issue or a date to what it cites."""
    found: dict[str, list[str]] = {}
    for path in sorted((repo / "tests").rglob("test_*.py")):
        cited = list(dict.fromkeys(CITATION.findall(path.read_text())))
        if cited:
            found[path.relative_to(repo).as_posix()] = cited
    return found


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    parser = ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"how far back to read master's failures (default {DEFAULT_DAYS})",
    )
    args = parser.parse_args(argv)
    until = today or datetime.now(UTC).date()
    since = until - timedelta(days=args.days)
    try:
        failing, unread = ci_failures.failing_test_modules(since, until, BRANCH)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(ci_failures.gh_failure(exc), file=sys.stderr)
        return FAILED
    cited = citing_checks()
    quiet = {path: refs for path, refs in cited.items() if path not in failing}
    print(
        f"{len(quiet)} of {len(cited)} check files that cite an issue or a date have no"
        f" {BRANCH} failure since {since}; the operator decides which go"
    )
    if unread:
        print(
            f"{unread} red {BRANCH} attempts had no readable log, so a file below may have"
            " failed in one of them"
        )
    for path, refs in quiet.items():
        print(f"{path}  {', '.join(refs)}")
    return OK


if __name__ == "__main__":
    sys.exit(main())
