#!/usr/bin/python3
"""The `gh-quota` command: GitHub API spend per session on this host, beside GitHub's own count.

`status` reads the ledgers gh_quota.py keeps under /var/lib/gh-quota, and `mode` prints whether
this host records. Installed as /usr/bin/gh-quota. docs/github-quota.md holds the design.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

# Appended, so a checkout's copy found earlier on the path wins in a test run.
sys.path.append("/usr/lib/homelab")
from gh_quota import (
    ATTENTION_SHARE,
    CONF,
    KEEP_S,
    LEDGER_DIR,
    Ledger,
    Window,
    Writes,
    ledger_for,
)

# GitHub's secondary limits on content-creating requests, for which it reports no count.
WRITES_PER_MINUTE = 80
WRITES_PER_HOUR = 500


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def render(
    windows: Sequence[Window], writes: Sequence[Writes], failures: int, *, every: bool
) -> list[str]:
    """The lines `gh-quota status` prints.

    A count line per window, naming every identity that shares it, with the sessions to look
    at under it, then each identity's
    recent writes against GitHub's secondary limits, then the ledger's failures.
    """
    lines = [] if windows else ["no GitHub call recorded in an open quota window"]
    for window in windows:
        over = window.over()
        shown = sorted(window.sessions, key=lambda s: -sum(window.sessions[s].values()))
        shown = shown if every else over
        hidden = len(window.sessions) - len(shown)
        reset = datetime.fromtimestamp(window.reset, UTC).strftime("%H:%M")
        share = round(100 * window.beyond / window.used) if window.used else 0
        line = (
            f"{window.identity} {window.bucket}: {window.used}/{window.quota} used, "
            f"resets {reset} UTC; {_count(len(window.sessions), 'session')} made "
            f"{_count(window.recorded, 'call')}; "
            f"{window.beyond} beyond the ledger ({share}%); "
            f"{len(over)} at or over {round(100 * ATTENTION_SHARE)}%"
        )
        lines.append(line + (f"; --all lists {hidden} more" if hidden else ""))
        for session in shown:
            calls = window.sessions[session]
            top = ", ".join(f"{name} x{n}" for name, n in calls.most_common(3))
            lines.append(
                f"  {session[:36]}  {_count(sum(calls.values()), 'call')}  {top}"
            )
    lines.extend(
        f"{w.identity} writes: {w.minute}/{WRITES_PER_MINUTE} in the last minute, "
        f"{w.hour}/{WRITES_PER_HOUR} in the last hour"
        for w in writes
    )
    if failures:
        lines.append(
            f"{failures} ledger failures in {KEEP_S // 86400} days; see {LEDGER_DIR}/failures.*"
        )
    return lines


def main(argv: Sequence[str] | None = None, ledger: Ledger | None = None) -> int:
    """Run `gh-quota`: `status` reports spend per session, `mode` prints whether the host records."""
    parser = argparse.ArgumentParser(
        prog="gh-quota",
        description="GitHub API spend per session on this host, beside GitHub's own count.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="spend per session in the open quota windows"
    )
    status.add_argument("--all", action="store_true", help="list every session")
    status.add_argument(
        "--json", action="store_true", help="print one compact JSON array"
    )
    commands.add_parser("mode", help="print `record` or `off`")
    opts = parser.parse_args(argv)

    ledger = ledger or ledger_for(os.environ)
    if opts.command == "mode":
        print(ledger.mode())
        return 0
    if ledger.mode() == "off":
        print(f"gh-quota is off on this host: {CONF} does not say mode=record")
        return 0
    windows = ledger.windows()
    writes = ledger.writes()
    if not opts.json:
        print("\n".join(render(windows, writes, ledger.failures(), every=opts.all)))
        return 0
    recent = {w.identity: w for w in writes}
    rows = [
        {
            "identity": w.identity,
            "bucket": w.bucket,
            "used": w.used,
            "quota": w.quota,
            "reset": w.reset,
            "recorded": w.recorded,
            "beyond": w.beyond,
            "sessions": w.sessions,
            "writes_minute": sum(recent[i].minute for i in w.identities if i in recent),
            "writes_hour": sum(recent[i].hour for i in w.identities if i in recent),
        }
        for w in windows
    ]
    print(json.dumps(rows, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
