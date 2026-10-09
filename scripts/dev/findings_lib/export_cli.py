"""The `export` subcommand: the whole `claude` register, every state, written as one JSON file.

The register lives only on GitHub, and the break-glass repo bundle is a `git bundle`, so it
carries no issue history. Losing GitHub would lose every refuted and accepted ruling, and the
next review would re-file them. `export` is the copy kept beside the bundle
(`docs/longhorn-disaster-recovery.md`, *The off-site recovery kit*).

WHY DATE SLICES. A label-filtered `gh issue list` goes through GitHub's search API, which stops
at 1000 issues whatever `--limit` says (`gh_calls.ISSUE_LIST_CAP`), and the register passed
1000 in 2026-10. `export` reads it in `created:` windows instead, each small enough to come
back whole. A window that returns the cap may have been truncated, so it is split in half and
read again; a single day at the cap cannot be split, and the export fails rather than write a
short file. The windows run from the day before the oldest issue to the day after today,
because `created:` dates are UTC and a window ending on the local date could drop an issue
filed late in the evening.

The remaining blind spot is the search index's lag: an issue filed seconds before the export
may not be indexed yet. Nothing here can see that, so the operator compares the count it
prints with the register's search total when completeness matters.
"""

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# DECIDED: imported as `dev.findings_lib.<leaf>`, never as bare siblings — see the marker in findings.py.
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import (
    HISTORY_FIELDS,
    ISSUE_LIST_CAP,
    REGISTER_FETCH_TIMEOUT,
    _warn_at_the_comment_cap,
)
from dev.findings_lib.issue_model import PR_REPO
from lib.json_types import as_object_list

# The first window's width. Measured 2026-10-09: 1410 issues over 39 days, the busiest
# stretch 340 in 9 days, so a week is well under the cap. A busier week only costs a split.
WINDOW_DAYS = 7


class SliceAtCapError(RuntimeError):
    """A single-day `created:` window returned the list cap, so it may be truncated."""


def _list(tools: FindingsTools, search: str, limit: int, fields: str) -> list[dict]:
    return as_object_list(
        tools.gh_json(
            "issue",
            "list",
            "--label",
            "claude",
            "--state",
            "all",
            "--limit",
            str(limit),
            "--search",
            search,
            "--json",
            fields,
            timeout=REGISTER_FETCH_TIMEOUT,
        ),
        "gh issue list --search",
    )


def oldest_created(tools: FindingsTools) -> date | None:
    """The UTC creation date of the register's oldest issue, or None when it is empty."""
    rows = _list(tools, "sort:created-asc", 1, "createdAt")
    if not rows:
        return None
    return datetime.fromisoformat(rows[0]["createdAt"]).date()


def read_window(
    tools: FindingsTools,
    start: date,
    end: date,
    progress: Callable[[str], None],
    cap: int = ISSUE_LIST_CAP,
) -> list[dict]:
    """Every register issue created from ``start`` to ``end`` inclusive, splitting at the cap.

    Raises:
        SliceAtCapError: a one-day window returned ``cap`` issues, `ISSUE_LIST_CAP` unless
            a test passes a smaller one.
    """
    issues = _list(
        tools,
        f"created:{start.isoformat()}..{end.isoformat()}",
        cap,
        HISTORY_FIELDS,
    )
    if len(issues) < cap:
        progress(f"{start}..{end}: {len(issues)} issues")
        return issues
    if start == end:
        raise SliceAtCapError(
            f"{cap} issues created on {start} alone, gh's list cap -- the day "
            "cannot be split by date, so the export would be incomplete"
        )
    mid = start + (end - start) // 2
    progress(f"{start}..{end}: at the {cap}-issue cap, splitting")
    return read_window(tools, start, mid, progress, cap) + read_window(
        tools, mid + timedelta(days=1), end, progress, cap
    )


def read_register(
    tools: FindingsTools,
    today: date,
    progress: Callable[[str], None],
    cap: int = ISSUE_LIST_CAP,
) -> list[dict]:
    """Every register issue in every state, deduplicated and sorted by number."""
    oldest = oldest_created(tools)
    if oldest is None:
        return []
    start, last = oldest - timedelta(days=1), today + timedelta(days=1)
    by_number: dict[int, dict] = {}
    while start <= last:
        end = min(start + timedelta(days=WINDOW_DAYS - 1), last)
        for issue in read_window(tools, start, end, progress, cap):
            by_number[issue["number"]] = issue
        start = end + timedelta(days=1)
    return _warn_at_the_comment_cap([by_number[n] for n in sorted(by_number)])


def _write_atomically(path: str, text: str) -> None:
    """Writes ``text`` to ``path`` through a sibling temp file, so a failure leaves no file."""
    fd, tmp = tempfile.mkstemp(
        dir=_Path(path).resolve().parent, prefix=".register-export-"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def cmd_export(
    args: argparse.Namespace,
    tools: FindingsTools,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    cap: int = ISSUE_LIST_CAP,
) -> int:
    """Handles the ``export`` subcommand: writes the whole register to ``args.out``.

    Args:
        args: parsed CLI namespace carrying ``out``.
        tools: the process boundaries the reads go through.
        now: the clock; a test pins it.
        cap: gh's list cap; a test lowers it to exercise the split.

    Returns:
        0 when the file was written or ``--dry-run`` was given, 1 when a one-day window hit
        the cap and nothing was written.
    """
    if args.dry_run:
        print(
            f"would write the whole register to {args.out}; read nothing, wrote nothing"
        )
        return 0
    stamp = now()

    def progress(line: str) -> None:
        sys.stderr.write(line + "\n")

    try:
        issues = read_register(tools, stamp.date(), progress, cap)
    except SliceAtCapError as exc:
        sys.stderr.write(f"export: {exc}; nothing written\n")
        return 1
    document = {
        "repo": tools.repo or PR_REPO,
        "exported_at": stamp.isoformat(timespec="seconds"),
        "count": len(issues),
        "issues": issues,
    }
    _write_atomically(args.out, json.dumps(document, indent=1) + "\n")
    print(f"wrote {len(issues)} issues to {args.out}")
    return 0
