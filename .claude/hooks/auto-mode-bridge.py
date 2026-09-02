#!/usr/bin/env python3
"""Two narrow bridges between auto mode and this repo, on two events one script serves.

`PermissionDenied` — fires only in auto mode, only when the classifier denied the call.
`./scripts/deploy_tools/gitops_tick.sh` is allow-listed and still denied about 1 run in 7 on
identical command text (measured 2026-08-22, recorded in CLAUDE.md). The denial is the
classifier's own variance, not a rule, so the fix is to let the model try once more rather than
to widen anything: `retry: true` tells it the call may be reissued, and the classifier judges the
reissue exactly as it judged the first. Two retries per session cap it, so a command the
classifier means to refuse still stops.

`PostToolUseFailure` — decodes the deploy wrapper's exit codes. 75, 4, 3 and 2 all mean NOTHING
WAS DEPLOYED, and each is a different next step, but they reach Claude as a bare `Exit code N`
line that reads like a playbook failure. 20 is the inverse case, added 2026-09-02 for issue #840:
the playbook ran and failed, so changes ARE live. CLAUDE.md says so in prose; this says it at the
moment it happens, which is the difference between reading the runbook and being told.

`classifierContext` is deliberately not used here. It is a PostToolUse field, and every fact
worth sending the classifier from this repo is either a failure (which lands on
PostToolUseFailure, where the field does not exist) or already stated in `autoMode.environment`
and `autoMode.allow`, where it is configuration rather than unverified application context.

Stdlib-only, like the other hooks here: it runs under `uv run` from the wrapper, and the test
suite loads it by path.
"""

from __future__ import annotations

import json
import os
import re
import sys

# A tick invocation and nothing else. A compound command that merely CONTAINS the tick is not
# covered: the classifier judged the whole line, and the part it objected to may be the other
# half. `cd <dir> && ` is allowed in front because that is how a worktree session reaches the
# primary checkout's copy.
_TICK = re.compile(
    r"^(?:cd\s+[^\s;&|]+\s*&&\s*)?(?:\./|/[^\s]*/)?scripts/deploy_tools/gitops_tick\.sh\s*$"
)

# Denials the classifier did not actually adjudicate. Claude Code ignores `retry` for the first
# of these already; the second is a model outage, where an immediate retry buys nothing. Matching
# them keeps the retry ledger honest — a no-verdict denial should not spend one of the two.
_NO_VERDICT_PREFIXES = (
    "Auto mode could not evaluate this action",
    "Classifier unavailable",
)

MAX_RETRIES_PER_SESSION = 2

# 75, 4, 3 and 2 each mean the deploy refused BEFORE touching anything; 20 is the opposite and
# says so in its own words. The text is the wrapper's own contract, kept in the same words
# CLAUDE.md uses so the two don't drift into two stories.
_DEPLOY_EXITS = {
    75: (
        "deploy.sh exit 75: the /var/lock/server-git-tree.lock stayed busy, so NOTHING was "
        "deployed. The GitOps timer or another session holds it. This is a resume point, not a "
        "playbook failure — re-run the same command shortly."
    ),
    4: (
        "deploy.sh exit 4: the tree is behind origin/master, so NOTHING was deployed. A stale "
        "tree renders stale templates and reverts live config while every repo-side check still "
        "reads green. Pull first; never --skip-staleness-check."
    ),
    3: (
        "deploy.sh exit 3: the change is broad (shared templates, inventory, the setup plane) "
        "and maps to no single service, so NOTHING was deployed. --changed refuses it by design."
    ),
    2: (
        "deploy.sh exit 2: a --tags value matched no service in containers_list, so NOTHING was "
        "deployed. --list-services prints every valid value."
    ),
    20: (
        "deploy.sh exit 20: the playbook RAN and a task failed, so this is the one deploy exit "
        "where changes ARE live — everything applied before the failing task took effect. Read "
        "the PLAY RECAP and the failing TASK; do not treat it as a tag, staleness or lock "
        "refusal, and do not assume a re-run is safe."
    ),
}

_DEPLOY_CMD = re.compile(r"(?:^|[\s;&|])(?:\./|/[^\s]*/)?scripts/deploy\.sh(?:\s|$)")
_EXIT_CODE = re.compile(r"^Exit code (\d+)", re.MULTILINE)


def ledger_path(session_id: str) -> str:
    """Where this session's retry count lives.

    `.claude/logs/` is gitignored and already the hooks' scratch dir, so the ledger doesn't have to
    invent a location outside the repo.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo, ".claude", "logs", f"auto-mode-retries-{session_id}.json")


def retries_used(path: str) -> int:
    """Retries already granted this session.

    An unreadable or malformed ledger counts as zero: the cap is a guard against a loop, and a
    broken ledger must not be a way to lose the feature silently — the loop it guards against needs
    the classifier to deny twice more, which the consecutive-block fallback stops on its own.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get("retries", 0)
    except OSError, ValueError, AttributeError:
        return 0
    return value if isinstance(value, int) and value >= 0 else 0


def record_retry(path: str, used: int) -> None:
    """Bump the ledger.

    Best-effort: a write that fails leaves the count where it was, which costs at most one extra
    retry and never blocks the decision.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"retries": used + 1}, fh)
    except OSError:
        pass


def should_retry(payload: dict) -> bool:
    """True when this denial is the known gitops-tick flake and the session has retries left."""
    if payload.get("tool_name") != "Bash":
        return False
    command = payload.get("tool_input", {}).get("command", "")
    if not _TICK.match(command.strip()):
        return False
    reason = payload.get("reason", "") or ""
    if reason.startswith(_NO_VERDICT_PREFIXES):
        return False
    path = ledger_path(str(payload.get("session_id", "unknown")))
    used = retries_used(path)
    if used >= MAX_RETRIES_PER_SESSION:
        return False
    record_retry(path, used)
    return True


def deploy_exit_note(payload: dict) -> str | None:
    """The resume-point meaning of a failed deploy.sh, or None when this isn't one.

    Keyed on the `Exit code N` first line, which the hook docs name as the stable part of the
    error string; everything after it is display text.
    """
    if payload.get("tool_name") != "Bash":
        return None
    if not _DEPLOY_CMD.search(payload.get("tool_input", {}).get("command", "")):
        return None
    if payload.get("is_interrupt"):
        return None
    match = _EXIT_CODE.search(payload.get("error", "") or "")
    if not match:
        return None
    return _DEPLOY_EXITS.get(int(match.group(1)))


def main() -> None:
    """Read the hook payload from stdin and bridge one PermissionDenied or PostToolUseFailure.

    For a PermissionDenied event, requests a retry (via `should_retry`) when the ledger
    hasn't already spent it on this session. For a PostToolUseFailure event, decodes a
    `deploy.sh` non-zero exit into an explanatory `additionalContext` note. Prints the
    corresponding hookSpecificOutput JSON and returns; any other payload is ignored.
    """
    try:
        payload = json.load(sys.stdin)
    except ValueError, OSError:
        return
    if not isinstance(payload, dict):
        return

    event = payload.get("hook_event_name")
    if event == "PermissionDenied":
        if should_retry(payload):
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PermissionDenied",
                            "retry": True,
                        }
                    }
                )
            )
    elif event == "PostToolUseFailure":
        note = deploy_exit_note(payload)
        if note:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUseFailure",
                            "additionalContext": note,
                        }
                    }
                )
            )


if __name__ == "__main__":
    main()
