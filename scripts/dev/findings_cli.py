"""The argparse construction for `findings.py`: every subparser, no boundary calls.

Pure argument-parsing, split out of `findings.py` to keep that file under its 600-line cap.
`findings.py` imports `_parser` from here as `dev.findings_cli` (never as a bare sibling),
the same DECIDED rule that governs its other cross-module imports — `scripts/docs/reference/
backlog.py` reaches `findings.py` with only `scripts/` on `sys.path`.
"""

import argparse
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.findings_model import DOMAINS, KINDS, SEVERITIES
from dev.findings_verify import DEFAULT_VERIFY_TIMEOUT


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


def _parser(description: str) -> argparse.ArgumentParser:
    """Build the CLI parser.

    Args:
        description: the top-level ``--help`` banner. Passed in rather than restated here
            so `findings.py`'s own docstring stays the one place that text is written.
    """
    p = argparse.ArgumentParser(description=description)
    _add_dry_run(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser(
        "open", help="file a finding, or touch/reopen the existing issue"
    )
    _add_dry_run(o, suppress=True)
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
        help="read-only command; exit 0 means fixed, non-zero means it still reproduces",
    )

    t = sub.add_parser(
        "touch", help="record a re-observation; the third adds escalated"
    )
    _add_dry_run(t, suppress=True)
    t.add_argument("number", type=int)
    t.add_argument("--source", default="session")

    cl = sub.add_parser("claim", help="claim issues for a worktree")
    _add_dry_run(cl, suppress=True)
    cl.add_argument("numbers", nargs="+", type=int)
    cl.add_argument("--worktree", required=True, help="the branch doing the work")
    cl.add_argument("--session", help="the Claude session id, for the thread to read")

    rl = sub.add_parser("release", help="release this worktree's claim")
    _add_dry_run(rl, suppress=True)
    rl.add_argument("numbers", nargs="+", type=int)
    rl.add_argument("--worktree", required=True)
    rl.add_argument("--reason", help="why, for the release comment")

    cs = sub.add_parser("claims", help="every open claim, live or stale")
    _add_dry_run(cs, suppress=True)
    cs.add_argument("--json", action="store_true")

    rp = sub.add_parser("reap", help="release every stale claim")
    _add_dry_run(rp, suppress=True)

    c = sub.add_parser("close", help="close as fixed, refuted or accepted")
    _add_dry_run(c, suppress=True)
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
        help="rows for the review skill and the docs generator; marks manual and "
        "claimed issues rather than hiding either",
    )
    _add_dry_run(ls, suppress=True)
    ls.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ls.add_argument("--json", action="store_true")

    sl = sub.add_parser("sync-labels", help="create any missing label")
    _add_dry_run(sl, suppress=True)

    v = sub.add_parser(
        "verify",
        help="re-run each finding's verify-by command and report fixed/still-open",
    )
    _add_dry_run(v, suppress=True)
    v.add_argument("numbers", nargs="*", type=int, help="issue numbers to verify")
    v.add_argument("--all", action="store_true", help="verify every open finding")
    v.add_argument(
        "--close", action="store_true", help="close passing findings as fixed"
    )
    v.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        help="seconds before a verify-by command counts as an error",
    )

    nx = sub.add_parser("next", help="issues a session may pick up, best first")
    _add_dry_run(nx, suppress=True)
    nx.add_argument("--limit", type=int, default=10)
    nx.add_argument("--json", action="store_true")

    return p
