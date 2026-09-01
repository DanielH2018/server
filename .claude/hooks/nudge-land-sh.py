#!/usr/bin/env python3
"""PreToolUse(Bash) guard: stop hand-polling CI when land.sh already waits for it.

THE PROBLEM. `scripts/deploy_tools/land.sh` exists so that merging a PR is followed through to
a verified deploy in one backgrounded command: it waits for master CI on the merge commit,
ticks, deploys what the tick deferred, and prints a VERDICT line. CLAUDE.md says so, and
records that hand-polling cost 835 polls across 213 wait episodes before it existed.

It is still happening. Over the seven days to 2026-08-29 the session transcripts hold 173
`gh pr checks`, 75 `gh run list` and 61 `gh run watch` calls against 29 invocations of
land.sh. A paragraph in CLAUDE.md has not closed that gap, so this hook does.

WHAT IT REFUSES, AND WHAT IT LEAVES ALONE. Two shapes, for two different reasons:

  1. A command that blocks on CI by itself -- `gh run watch`, or `gh pr checks --watch`. There
     is no one-shot reading of these: they exist to wait, which is land.sh's job.

  2. The third and later CI-status read in one session. Glancing at a PR's checks once or
     twice is ordinary work; doing it repeatedly is a poll loop spelled out by hand. The count
     lives in a per-session file, so a fresh session starts with its two free reads back.

Everything else passes untouched -- `gh pr view`, `gh pr merge`, `gh api`, and the first two
status reads. The hook can only ever DENY; it never approves, so it cannot widen what the
classifier would otherwise allow.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision naming the land.sh form to
use instead; otherwise no output -> normal permission flow.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

from _hook_common import emit_pretooluse_decision, invokes, split_stages

# Reads that answer "what is CI doing right now". `gh pr view` and `gh api` are absent on
# purpose: both are general-purpose and used for far more than CI status.
_STATUS_COMMANDS = (
    ("gh", "pr", "checks"),
    ("gh", "run", "list"),
    ("gh", "run", "view"),
)

# Commands whose whole purpose is to block until CI finishes.
_WATCH_COMMANDS = (("gh", "run", "watch"),)

# Two status reads are a glance; the third is a loop. Deliberately low -- land.sh costs one
# command, so the bar for reaching for it should be low too.
_FREE_READS = 2

# A session's counter is worthless once the session is over, and /tmp is shared between
# parallel background jobs, so the file is keyed by session id.
_COUNTER_TTL_S = 24 * 3600

_LAND = (
    "Use ./scripts/deploy_tools/land.sh --pr <n> --since <pre-merge-sha> instead, as ONE "
    "backgrounded command with stdout and stderr redirected to a file (Ansible refuses the "
    "harness's non-blocking pipe). It waits for master CI on the merge commit, ticks, deploys "
    "what the tick deferred, and prints a VERDICT: line. See the land-after-merge skill."
)


def classify(command: str) -> str | None:
    """ "watch", "status", or None -- what kind of CI polling this command is."""
    for stage in split_stages(command):
        if any(invokes(stage, p) for p in _WATCH_COMMANDS):
            return "watch"
        if any(invokes(stage, p) for p in _STATUS_COMMANDS):
            # `--watch` turns a one-shot read into a blocking wait.
            if "--watch" in stage or "-w" in stage:
                return "watch"
            return "status"
    return None


def _counter_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return Path(tempfile.gettempdir()) / f"claude-ci-poll-{safe}"


def bump(session_id: str, now: float | None = None) -> int:
    """Record one status read for this session and return the running count.

    A counter file older than the TTL is treated as a new session's -- session ids are unique,
    but a stale file left by a crashed run would otherwise deny a fresh session's first read.
    """
    now = time.time() if now is None else now
    path = _counter_path(session_id)
    count = 0
    try:
        stamp, raw = path.read_text().split(None, 1)
        if now - float(stamp) < _COUNTER_TTL_S:
            count = int(raw)
    except OSError, ValueError:
        count = 0
    count += 1
    try:
        path.write_text(f"{now} {count}")
    except OSError:
        # An unwritable temp dir must not break the session; without a counter the hook
        # degrades to catching only the blocking forms, which is still the worse half.
        pass
    return count


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return 0

    # land.sh runs gh itself. Its own invocation is the fix, never the problem.
    if "land.sh" in command:
        return 0

    kind = classify(command)
    if kind is None:
        return 0

    if kind == "watch":
        emit_pretooluse_decision(
            "deny",
            "This command blocks until CI finishes, which is what land.sh already does. "
            + _LAND,
        )
        return 0

    count = bump(str(payload.get("session_id", "")))
    if count > _FREE_READS:
        emit_pretooluse_decision(
            "deny",
            f"This is CI status read #{count} in this session -- a poll loop written by "
            "hand. " + _LAND,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
