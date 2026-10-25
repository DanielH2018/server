#!/usr/bin/env python3
"""Restart the background Claude sessions a shutdown killed, so their Remote Control links return.

`claude-bg-resume.service` runs this once at boot as the operator. Claude Code's background
supervisor runs on demand, and after a reboot it shows each session the shutdown killed as
failed until someone attaches, replies or runs `claude respawn`. A session whose process is gone
also has no Remote Control connection, so its phone entry stays dead until then.

The candidates are the workers the previous supervisor last recorded as running, in
`~/.claude/daemon/roster.json`, that `claude agents --json --all` still lists as background
sessions with no live process. A session the supervisor had already stopped for idling is not in
the roster, so it is left alone, exactly as it was before the reboot.

The candidate list is read in full before the first respawn. The first `claude respawn` starts a
supervisor, and that supervisor rewrites the roster with only the workers alive in it, which
would drop every other candidate.

A roster older than `MAX_ROSTER_AGE_S` is skipped whole. Claude Code itself asks before it
resumes a session that ended more than 48 hours ago, and this script follows the same line.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_ROSTER_AGE_S = 48 * 3600
API_HOST = "api.anthropic.com"
RESPAWN_TIMEOUT_S = 180


def candidates(roster: dict, sessions: list[dict], now_ms: int) -> list[str]:
    """Return the short IDs to respawn, in roster order.

    `roster` is the parsed `roster.json` and `sessions` the parsed `claude agents --json --all`.
    An ID qualifies when the roster lists it as a worker and the session list shows it as a
    background session with no `pid`. A roster whose `updatedAt` is more than
    `MAX_ROSTER_AGE_S` before `now_ms` yields nothing.
    """
    updated_at = roster.get("updatedAt")
    if not isinstance(updated_at, (int, float)):
        return []
    if now_ms - updated_at > MAX_ROSTER_AGE_S * 1000:
        return []
    dead = {
        s["id"]
        for s in sessions
        if s.get("kind") == "background" and "id" in s and "pid" not in s
    }
    return [short for short in roster.get("workers", {}) if short in dead]


def wait_for_dns(
    host: str,
    budget_s: float,
    resolve: Callable[[str], object] = lambda h: socket.getaddrinfo(h, 443),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Return True once `host` resolves, or False when `budget_s` elapses first.

    claude-rc.service lost this race at boot on 2026-10-25 (`getaddrinfo ENOTFOUND`), and a
    session respawned before DNS works fails the same way.
    """
    deadline = clock() + budget_s
    while True:
        try:
            resolve(host)
            return True
        except OSError:
            if clock() >= deadline:
                return False
            sleep(5)


def list_sessions(claude: str) -> list[dict]:
    """Return `claude agents --json --all`, which reads state without starting a supervisor."""
    out = subprocess.run(
        [claude, "agents", "--json", "--all"],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    ).stdout
    return json.loads(out)


def respawn(claude: str, short: str) -> bool:
    """Run `claude respawn <short>` and return whether it exited 0."""
    result = subprocess.run(
        [claude, "respawn", short],
        stdin=subprocess.DEVNULL,
        timeout=RESPAWN_TIMEOUT_S,
        check=False,
    )
    return result.returncode == 0


@dataclass(frozen=True)
class Tools:
    """The boundaries `main` reaches through, replaced by fakes in the tests."""

    list_sessions: Callable[[str], list[dict]] = list_sessions
    respawn: Callable[[str, str], bool] = respawn
    wait_for_dns: Callable[[str, float], bool] = wait_for_dns
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)


def main(argv: list[str] | None = None, tools: Tools | None = None) -> int:
    tools = tools or Tools()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", required=True, help="path to the claude launcher")
    parser.add_argument(
        "--roster",
        type=Path,
        default=Path.home() / ".claude" / "daemon" / "roster.json",
    )
    parser.add_argument("--dns-budget", type=float, default=300.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the sessions that would be respawned and respawn nothing",
    )
    args = parser.parse_args(argv)

    try:
        roster = json.loads(args.roster.read_text())
    except FileNotFoundError:
        print(f"no roster at {args.roster}; nothing to resume")
        return 0
    todo = candidates(roster, tools.list_sessions(args.claude), tools.now_ms())
    print(f"{len(todo)} session(s) to respawn: {' '.join(todo) or '-'}")
    if args.dry_run or not todo:
        return 0

    if not tools.wait_for_dns(API_HOST, args.dns_budget):
        print(
            f"{API_HOST} did not resolve within {args.dns_budget:.0f}s; respawned nothing"
        )
        return 1
    failed = [short for short in todo if not tools.respawn(args.claude, short)]
    if failed:
        print(f"respawn failed for: {' '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
