"""The argparse construction for `findings.py`: every subparser, no boundary calls.

Pure argument-parsing, split out of `findings.py` to keep that file under its 600-line cap.
`findings.py` imports `_parser` from here as `dev.findings_lib.cli` (never as a bare sibling),
the same DECIDED rule that governs its other cross-module imports — `scripts/docs/reference/
backlog.py` reaches `findings.py` with only `scripts/` on `sys.path`.
"""

import argparse
from datetime import date
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import DOMAINS, KINDS, SEVERITIES

# How many search hits `history` reads back unless `--limit` says otherwise. A topic search
# that needs more than this is too broad to read, and `cmd_history` warns when it fills.
HISTORY_LIMIT = 30


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add ``--dry-run`` to ``parser``.

    Every subparser gets its own copy so the flag parses on either side of the
    subcommand name — argparse only accepts a parent-parser optional before the
    subcommand token. ``suppress=True`` (used on the subparsers) sets
    ``default=argparse.SUPPRESS`` so an absent subparser flag leaves the top-level
    parser's own default in place instead of overwriting it back to ``False``.
    """
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="print the gh commands, write nothing",
    )


def _add_repo(parser: argparse.ArgumentParser) -> None:
    """Add ``--repo`` to ``parser``: which repo's `claude` register the subcommand reads and writes.

    On every subparser rather than the root, for the reason `_add_dry_run` gives, with
    ``SUPPRESS`` so an absent flag leaves the root's None in place.
    """
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        default=argparse.SUPPRESS,
        help="aim every read and write at another repo's `claude` register (the dotfiles "
        "repo, say); claim, claims, reap and next also judge claims against that repo's "
        "local checkout",
    )


def _parser(description: str) -> argparse.ArgumentParser:
    """Build the CLI parser.

    Args:
        description: the top-level ``--help`` banner. Passed in rather than restated here
            so `findings.py`'s own docstring stays the one place that text is written.
    """
    p = argparse.ArgumentParser(description=description)
    _add_dry_run(p, suppress=False)
    p.add_argument("--repo", metavar="OWNER/NAME", help="see any subcommand's --help")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser(
        "open", help="file a finding, or touch/reopen the existing issue"
    )
    _add_dry_run(o, suppress=True)
    _add_repo(o)
    o.add_argument("--title", required=True)
    o.add_argument("--body-file", required=True, type=Path)
    o.add_argument("--severity", required=True, choices=SEVERITIES)
    o.add_argument("--kind", required=True, choices=KINDS)
    o.add_argument("--domain", choices=DOMAINS)
    o.add_argument(
        "--file",
        help="primary file:line the finding cites; the line is dropped from the fingerprint",
    )
    o.add_argument("--source", default="session", help="review-<date> or session")
    o.add_argument("--no-vetted-remediation", action="store_true")
    o.add_argument(
        "--verify-by",
        help="prose describing how to check whether this finding is fixed; `verify` prints "
        "it back and runs nothing",
    )
    o.add_argument(
        "--not-before",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="withhold from `next` and `claim` until this date; for a finding whose own "
        "precondition (a metric window, a cron firing) is not met before it",
    )
    o.add_argument(
        "--manual",
        action="store_true",
        help="reserve for the operator: `next` withholds it and `claim` refuses it. On an "
        "issue the dedup matches, adds the label there too",
    )
    o.add_argument(
        "--review-leftover",
        action="store_true",
        help="a finding a fan-out review left after its fix round; `next` offers it after "
        "every fresh issue (#3958)",
    )

    df = sub.add_parser(
        "defer",
        help="withhold an issue from `next` and `claim` until a date, or clear that",
    )
    _add_dry_run(df, suppress=True)
    _add_repo(df)
    df.add_argument("number", type=int)
    when = df.add_mutually_exclusive_group(required=True)
    when.add_argument(
        "--until",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="the first day the issue may be offered again; replaces any earlier date",
    )
    when.add_argument(
        "--clear", action="store_true", help="remove the not-before date now"
    )

    mn = sub.add_parser(
        "manual",
        help="reserve an issue for the operator, releasing any claim on it, or clear that",
    )
    _add_dry_run(mn, suppress=True)
    _add_repo(mn)
    mn.add_argument("number", type=int)
    mn.add_argument(
        "--clear", action="store_true", help="remove `manual`; `next` offers it again"
    )

    t = sub.add_parser(
        "touch", help="record a re-observation; the third adds escalated"
    )
    _add_dry_run(t, suppress=True)
    _add_repo(t)
    t.add_argument("number", type=int)
    t.add_argument("--source", default="session")

    cl = sub.add_parser("claim", help="claim issues for a worktree")
    _add_dry_run(cl, suppress=True)
    _add_repo(cl)
    cl.add_argument("numbers", nargs="+", type=int)
    cl.add_argument("--worktree", required=True, help="the branch doing the work")
    cl.add_argument("--session", help="the Claude session id, for the thread to read")
    cl.add_argument(
        "--force",
        action="store_true",
        help="claim even when --worktree names no branch, or one whose state makes the "
        "claim read stale the moment it lands",
    )

    rl = sub.add_parser("release", help="release this worktree's claim")
    _add_dry_run(rl, suppress=True)
    _add_repo(rl)
    rl.add_argument("numbers", nargs="*", type=int)
    rl.add_argument(
        "--all",
        action="store_true",
        help="release every open claim --worktree holds, in place of issue numbers",
    )
    rl.add_argument("--worktree", required=True)
    rl.add_argument("--reason", help="why, for the release comment")

    cs = sub.add_parser("claims", help="every open claim, live or stale")
    _add_dry_run(cs, suppress=True)
    _add_repo(cs)
    cs.add_argument("--json", action="store_true")
    cs.add_argument(
        "--worktree",
        help="only the claims this branch holds, plus those held by the batch branches "
        "its `fanout.py place launch` runs started in this register",
    )

    rp = sub.add_parser("reap", help="release every stale claim")
    _add_dry_run(rp, suppress=True)
    _add_repo(rp)

    c = sub.add_parser("close", help="close as fixed, refuted or accepted")
    _add_dry_run(c, suppress=True)
    _add_repo(c)
    c.add_argument("number", type=int)
    how = c.add_mutually_exclusive_group(required=True)
    how.add_argument("--fixed", action="store_true", help="a change fixed it")
    how.add_argument(
        "--refuted", action="store_true", help="a skeptic disproved the finding"
    )
    how.add_argument(
        "--accepted",
        action="store_true",
        help="true, but the operator chose to live with the trade-off; never reopened",
    )
    c.add_argument("--pr", type=int, help="the PR that fixed it")
    c.add_argument(
        "--reason",
        help="required with --refuted (what disproved it) and with --accepted (why the "
        "trade-off stands)",
    )

    ls = sub.add_parser(
        "list",
        help="the open findings, for the review skill; marks manual, deferred and claimed "
        "issues rather than hiding any. Past findings are `history`",
    )
    _add_dry_run(ls, suppress=True)
    _add_repo(ls)
    ls.add_argument("--json", action="store_true")

    hs = sub.add_parser(
        "history",
        help="search findings in any state by topic or cited file; prints how each closed",
    )
    _add_dry_run(hs, suppress=True)
    _add_repo(hs)
    hs.add_argument(
        "terms", nargs="*", help="gh search terms, matched against the issue"
    )
    hs.add_argument(
        "--file",
        help="only findings whose body cites this path (a directory matches the files under "
        "it); a `:line` suffix is dropped",
    )
    hs.add_argument(
        "--limit",
        type=int,
        default=HISTORY_LIMIT,
        help=f"search hits to read (default {HISTORY_LIMIT}); a full page warns on stderr",
    )
    hs.add_argument("--json", action="store_true")

    sh = sub.add_parser(
        "show", help="one finding's body, outcome, closing PR and comment thread"
    )
    _add_dry_run(sh, suppress=True)
    _add_repo(sh)
    sh.add_argument("number", type=int)
    sh.add_argument("--json", action="store_true")
    sh.add_argument(
        "--brief",
        action="store_true",
        help="print only the deploy plane and the tests the issue's cited paths imply",
    )

    ex = sub.add_parser(
        "export",
        help="write every finding in every state to one JSON file, for the break-glass kit",
    )
    _add_dry_run(ex, suppress=True)
    _add_repo(ex)
    ex.add_argument("--out", required=True, help="the JSON file to write")

    sl = sub.add_parser("sync-labels", help="create any missing label")
    _add_dry_run(sl, suppress=True)
    _add_repo(sl)

    v = sub.add_parser(
        "verify",
        help="print each finding's stored instructions for how to verify it; runs nothing",
    )
    _add_dry_run(v, suppress=True)
    _add_repo(v)
    v.add_argument("numbers", nargs="*", type=int, help="issue numbers to report on")
    v.add_argument("--all", action="store_true", help="report on every open finding")

    nx = sub.add_parser("next", help="issues a session may pick up, best first")
    _add_dry_run(nx, suppress=True)
    _add_repo(nx)
    # No default bound. A default of 10 would truncate silently: an orchestrator reading
    # `next --json` would take 10 rows for the whole free set while more sat invisible. A
    # view blind to real state that does not announce it is a silent failure.
    # `rows[:None]` returns every row, so the slice in `cmd_next` needs no branch.
    nx.add_argument(
        "--limit",
        type=int,
        default=None,
        help="show at most N issues; every pickable issue by default",
    )
    nx.add_argument("--json", action="store_true")

    return p
