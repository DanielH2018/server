#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which run-hook.sh runs through `uv run python`
"""PreToolUse(Bash) guard: stop hand-polling CI when land.sh already waits for it.

THE PROBLEM. `scripts/deploy_tools/land.sh` exists so that merging a PR is followed through to
a verified deploy in one backgrounded command: it waits for master CI on the merge commit,
ticks, deploys what the tick deferred, and prints a VERDICT line. CLAUDE.md says so.

A paragraph in CLAUDE.md does not close that gap, so this hook does.

WHAT IT REFUSES, AND WHAT IT LEAVES ALONE. Two shapes, for two different reasons:

  1. A command that blocks on CI by itself -- `gh run watch`, or `gh pr checks --watch`. There
     is no one-shot reading of these: they exist to wait, which is land.sh's job.

  2. The third and later CI-status read in one session. Glancing at a PR's checks once or
     twice is ordinary work; doing it repeatedly is a poll loop spelled out by hand. The count
     lives in a per-session file, so a fresh session starts with its two free reads back.

Everything else passes untouched -- `gh pr view`, `gh pr merge`, `gh api`, and the first two
status reads. So does a read aimed at another repository (`--repo`/`-R` or a GitHub URL naming
one), since land.sh lands only this repo's PRs and its advice is wrong elsewhere. So
does `gh run view --log`/`--log-failed`, which reads a finished run's log rather than polling
a running one. The hook can only ever DENY; it never approves, so it cannot widen what the
classifier would otherwise allow.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision naming the land.sh form to
use instead; otherwise no output -> normal permission flow.
"""

import json
import re
import sys
import tempfile
import time
from pathlib import Path

from _hook_common import Unsplittable, emit_pretooluse_decision, invokes, split_stages

# Reads that answer "what is CI doing right now". `gh pr view` and `gh api` are absent on
# purpose: both are general-purpose and used for far more than CI status.
_STATUS_COMMANDS = (
    ("gh", "pr", "checks"),
    ("gh", "run", "list"),
    ("gh", "run", "view"),
)

# The repository land.sh lands. A read that names any other one is out of this hook's scope:
# land.sh lands only this repo's PRs, and a refused agent would switch to `gh api` poll loops
# the hook cannot see.
_THIS_REPO = ("danielh2018", "server")

# `gh run view` flags that read a finished run's log. Neither polls: gh refuses them while
# the run is still in progress.
_LOG_FLAGS = ("--log", "--log-failed")

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


def _repo_of(value: str) -> tuple[str, str] | None:
    """The lower-cased `(owner, repo)` a `--repo` value or a GitHub URL names, or None.

    `--repo` takes `[HOST/]OWNER/REPO`; a URL is `https://github.com/OWNER/REPO/...`.
    """
    value = re.sub(r"^https?://", "", value)
    parts = [p for p in value.split("/") if p]
    if value.startswith("github.com/") or "." in (parts[0] if parts else ""):
        parts = parts[1:]
    if len(parts) < 2:
        return None
    return parts[0].lower(), parts[1].lower()


def names_another_repo(stage: list[str]) -> bool:
    """True when `stage` targets a repository other than this one.

    Reads `--repo X`, `--repo=X`, `-R X`, `-RX`, and a `https://github.com/...` argument
    (`gh pr checks` and `gh run view` both take a URL). A stage naming no repo is this
    repo's: gh resolves it from the working directory, which is here.
    """
    named: list[str] = []
    for i, word in enumerate(stage):
        if word in ("--repo", "-R") and i + 1 < len(stage):
            named.append(stage[i + 1])
        elif word.startswith("--repo="):
            named.append(word.split("=", 1)[1])
        elif word.startswith("-R") and len(word) > 2 and not word.startswith("--"):
            named.append(word[2:])
        elif word.startswith(("https://github.com/", "http://github.com/")):
            named.append(word)
    repos = {r for r in map(_repo_of, named) if r is not None}
    return any(r != _THIS_REPO for r in repos)


def classify(command: str, split=split_stages) -> str | None:
    """What kind of CI polling this command is: "watch", "status", or None.

    A command the splitter cannot read is None. The deny guards with a real cost
    (`block-footguns.py`, `block-protected-bash.py`) turn that into an `ask`; a missed nudge
    costs one hand-written poll, which is not worth a prompt on every unreadable command or,
    on a host without the `claude_guard` deploy, on every command.
    """
    try:
        stages = split(command)
    except Unsplittable:
        return None
    for stage in stages:
        if names_another_repo(stage):
            continue
        if any(invokes(stage, p) for p in _WATCH_COMMANDS):
            return "watch"
        if any(invokes(stage, p) for p in _STATUS_COMMANDS):
            if invokes(stage, ("gh", "run", "view")) and any(
                w in _LOG_FLAGS for w in stage
            ):
                continue
            # `--watch` turns a one-shot read into a blocking wait. Only the long form: on
            # both `gh pr checks` and `gh run view`, `-w` is `--web`, which opens a browser
            # and returns, and `gh pr checks` has no short form of `--watch` at all.
            if "--watch" in stage:
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


def decision(payload) -> tuple[str, str] | None:
    """The `(decision, reason)` pair for this payload, or None.

    Denies a command that blocks on CI outright, and denies the third or later CI-status
    read in the session, naming `land.sh` in both cases. The counter `bump` keeps is a side
    effect, so this runs at most once per payload: calling it twice would move the threshold
    from the third read to the second. The arm entry point `bash-pretool.py` calls; `main()`
    below is the same arm run as its own process.
    """
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return None

    # land.sh runs gh itself. Its own invocation is the fix, never the problem.
    if "land.sh" in command or "land.py" in command:
        return None

    kind = classify(command)
    if kind is None:
        return None

    if kind == "watch":
        return (
            "deny",
            "This command blocks until CI finishes, which is what land.sh already does. "
            + _LAND,
        )

    count = bump(str(payload.get("session_id", "")))
    if count > _FREE_READS:
        return (
            "deny",
            f"This is CI status read #{count} in this session -- a poll loop written by "
            "hand. " + _LAND,
        )
    return None


def main() -> int:
    """Read the hook payload from stdin and deny a CI-status command `decision` flags.

    Always returns 0; a deny is expressed through emitted JSON, not the exit code.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    verdict = decision(payload)
    if verdict:
        emit_pretooluse_decision(*verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
