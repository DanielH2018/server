#!/usr/bin/env python3
"""Copy each finding's `domain/<name>` label into its Domain issue field.

Slice 1 of `docs/github-label-audit.md`. It reads what the backlog page renders, the open
findings and the closed ones it lists, and sets Domain on each issue that carries a `domain/`
label and holds no Domain value yet. A run that stops partway is repaired by running it again,
because a filled issue is skipped, and a failed write exits non-zero with gh's error.

Each write is a `set_fields` call, a read then a mutation, and the mutations are spaced by
board-sync's `MUTATION_PAUSE_S`, which keeps a backfill of a few hundred issues under GitHub's
secondary write limit.

Usage:
    uv run python scripts/dev/fill_domain.py [--dry-run] [--repo DanielH2018/dotfiles]
"""

import sys
import time
from collections.abc import Callable

from dev.findings_lib.board import MUTATION_PAUSE_S
from dev.findings_lib.boundaries import FindingsTools, aimed
from dev.findings_lib.gh_calls import load_backlog_issues, set_fields
from dev.findings_lib.issue_model import DOMAIN_FIELD, DOMAINS, Issue, _prefixed
from lib.cli_help import ArgumentParser
from lib.exit_codes import OK

# TODO: https://github.com/DanielH2018/server/issues/9120 - delete with the domain/ labels.


def unfilled(issues: list[Issue]) -> list[tuple[Issue, str]]:
    """Each issue whose `domain/` label names a domain its Domain field does not hold yet."""
    todo = []
    for issue in issues:
        domain = _prefixed(issue.labels, "domain/")
        if domain in DOMAINS and not issue.fields.get(DOMAIN_FIELD):
            todo.append((issue, domain))
    return sorted(todo, key=lambda pair: pair[0].number)


def main(
    argv: list[str] | None = None,
    tools: FindingsTools | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fill the Domain field from the labels and print one count line.

    Args:
        argv: the command line; `sys.argv[1:]` when None.
        tools: the gh boundaries; the real ones when None.
        sleep: the pause between mutations; a test passes a recorder.
    """
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the writes, make none"
    )
    parser.add_argument(
        "--repo", help="OWNER/NAME of another register, such as dotfiles"
    )
    args = parser.parse_args(argv)
    tools = aimed(tools or FindingsTools(), args.repo)
    todo = unfilled(load_backlog_issues(tools))
    for k, (issue, domain) in enumerate(todo):
        if k and not args.dry_run:
            sleep(MUTATION_PAUSE_S)
        set_fields(issue.number, {DOMAIN_FIELD: domain}, tools, args.dry_run)
    verb = "would fill" if args.dry_run else "filled"
    print(f"fill-domain: {len(todo)} issue(s) {verb}")
    return OK


if __name__ == "__main__":
    sys.exit(main())
