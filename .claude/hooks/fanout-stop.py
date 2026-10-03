#!/usr/bin/env python3
# gen-hooks: register
#   event: Stop
#   timeout: 10
#   order: 10
"""Stop hook: keep a headless fan-out session going until it names a PR or a blocker.

THE PROBLEM. A fan-out batch is one `claude -p` process under a transient systemd unit
(`scripts/dev/fanout_lib/launch.py`). When the model ends a turn with text and no tool call,
the process exits there, whatever that text says. On long tasks Opus writes progress reports
as it goes ("Next I will open the PR"), and some of those end the turn. Nothing resumed the
session, so the batch stopped mid-task. `fanout_place.py status` then reported it `done`,
because `done` required only a non-empty final text (issue #2816). Two earlier incidents of the
same shape, #1291 and #2683, were closed with brief text only.

WHAT IT CHECKS. The completion condition the brief states in its *Finishing* section: the final
message carries a PR URL, or a line that starts with `needs input:` or `failed:` and names the
blocker. A batch whose brief tells it to land — the daniel-box brief, which carries the
`land.sh` command — owes a `VERDICT:` line with that PR URL, in the message or in a
`.fanout/land<n>.log`; a PR URL alone there means `gh pr create` returned and nothing more
(issue #2890). All three patterns mirror `scripts/dev/fanout_lib/status.py`, which reads the same final
text to decide `done`. A hook that let a session stop on a text `status` then reads as
unfinished would be checking a different condition from the one reported.
`test_the_hook_and_status_read_the_same_patterns` holds the pair equal. The hooks stay
stdlib-only, so the patterns are copied, not imported.

DECIDED: the check reads the final text and never asks GitHub whether the branch has a PR.
`status` reads the final text too, and a PR that exists but that the final text omits reads
as unfinished there. A blocked stop in that case costs one turn, and the model answers it by
printing the URL. The text check also needs no network call and no `gh` auth inside a hook.

WHERE IT RUNS. Only in a fan-out worktree, meaning `.fanout/brief.md` exists at or above the
payload's `cwd`. That is the marker `launch.py` writes and the dotfiles `worktree-landed.sh`
already keys on. Everywhere else the hook prints nothing.

THE CAP. A counter in `.fanout/stop-blocks` allows at most `MAX_BLOCKS` blocks per batch.
After that it lets the session end, and `status` reports the batch `no-pr` rather than `done`.
Anthropic's Opus 5.5 guide recommends stopping after two or three automatic continuations. A
model that cannot finish after three reminders needs an operator, not a fourth reminder.

Verified 2026-09-28 with a probe hook under `claude -p`: a Stop hook fires, a `block` decision
continues the session, and the continuation's reply becomes the `result` in the JSON report.

Reads the Stop payload on stdin. Emits `{"decision": "block", "reason": ...}` to continue the
session, or nothing to let it stop.
"""

import json
import re
import sys
from pathlib import Path

from _hook_common import read_payload

MARKER = Path(".fanout") / "brief.md"
COUNTER = Path(".fanout") / "stop-blocks"
MAX_BLOCKS = 3

# Mirrors `status.PR_URL`, `status.BLOCKER` and `status.VERDICT`.
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
BLOCKER = re.compile(r"(?im)^(?:needs input|failed):")
VERDICT = re.compile(r"(?m)^VERDICT:")
# The command only a landing brief carries. A batch placed on daniel-server is told to stop at
# `gh pr create`, so its brief does not hold this line. Keying on the brief rather than on the
# hostname asks what THIS batch was told to do, which is the thing the hook is checking.
LANDING_MARKER = "./scripts/deploy_tools/land.sh"
LAND_LOGS = "land*.log"


def fanout_root(cwd: str) -> Path | None:
    """The fan-out worktree at or above `cwd`, or None outside one."""
    start = Path(cwd)
    for directory in (start, *start.parents):
        if (directory / MARKER).is_file():
            return directory
    return None


def _landed(root: Path) -> bool:
    """Whether any `land<n>.log` in the batch's `.fanout/` holds a VERDICT line.

    The log is the landing's own record, so a batch that landed correctly and then reported
    tersely is finished even though its final message omits the verdict. Without this
    fallback the verdict rule would be a new way to trap a batch that did the work.
    """
    try:
        logs = sorted((root / ".fanout").glob(LAND_LOGS))
    except OSError:
        return False
    for log in logs:
        try:
            if VERDICT.search(log.read_text(errors="replace")):
                return True
        except OSError:
            continue
    return False


def open_item(
    message: str, root: Path | None = None, lands: bool = False
) -> str | None:
    """What the final message still owes, or None when it meets the completion condition.

    Args:
        message: the session's final assistant message.
        root: the fan-out worktree, read for a `land<n>.log` when a landing is owed.
        lands: whether this batch's brief tells it to land the PR with `land.sh`. On the
            landing host a PR URL alone is not a finish — `gh pr create` returning says
            nothing about whether the PR merged and deployed.
    """
    if BLOCKER.search(message):
        return None
    if not PR_URL.search(message):
        return (
            "your final message carries neither a PR URL nor a line starting `needs input:` "
            "or `failed:`"
        )
    if not lands:
        return None
    if VERDICT.search(message) or (root is not None and _landed(root)):
        return None
    return (
        "your final message names a PR but no `VERDICT:` line, and no `land<n>.log` in "
        "`.fanout/` holds one — the PR is open and the landing is not finished"
    )


def owes_a_landing(root: Path) -> bool:
    """Whether this batch's brief tells it to land its PR (the daniel-box brief)."""
    try:
        return LANDING_MARKER in (root / MARKER).read_text(errors="replace")
    except OSError:
        return False


def _blocks_so_far(root: Path) -> int:
    try:
        return int((root / COUNTER).read_text().strip() or 0)
    except OSError, ValueError:
        return 0


def decide(payload: dict) -> str | None:
    """The block reason for this Stop, or None to let the session end."""
    root = fanout_root(str(payload.get("cwd") or "."))
    if root is None:
        return None
    item = open_item(
        str(payload.get("last_assistant_message") or ""), root, owes_a_landing(root)
    )
    if item is None:
        return None
    count = _blocks_so_far(root)
    if count >= MAX_BLOCKS:
        return None
    try:
        (root / COUNTER).write_text(f"{count + 1}\n")
    except OSError:
        # Without a writable counter the cap cannot hold, so the hook must not block at all.
        return None
    return (
        f"This is a headless fan-out batch, and ending the turn ends the batch. The open item: "
        f"{item}. That text reads as a progress report, not a finish. If work remains, "
        "continue it now with tool calls: open the PR, and where the brief says to land it, "
        "run `land.sh` and wait for its verdict. When you are done, end with the PR URL as "
        "the last line, quoting the `VERDICT:` line where the brief asks for it. If you "
        "cannot finish, end with one line starting `needs input:` or `failed:` that names "
        "the blocker. "
        f"(Automatic continuation {count + 1} of {MAX_BLOCKS}.)"
    )


def main(stdin=sys.stdin, stdout=sys.stdout) -> int:
    payload = read_payload(stdin)
    if payload is None:
        return 0
    reason = decide(payload)
    if reason:
        stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
