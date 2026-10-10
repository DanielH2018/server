#!/usr/bin/env python3
# gen-hooks: register
#   event: PostToolUseFailure
#   matcher: Bash
#   timeout: 10
#   order: 10
#   args: --project
# gen-hooks: register
#   event: PermissionDenied
#   matcher: Bash
#   timeout: 10
#   order: 10
#   args: --project
"""Two narrow bridges between auto mode and this repo, on two events one script serves.

Usage: Claude Code runs this hook on the PermissionDenied:Bash and PostToolUseFailure:Bash
events, passing `--project`. It reads the hook JSON payload on stdin. It prints one
`hookSpecificOutput` JSON object (a retry request, or an `additionalContext` note) or nothing, and
it always exits 0. `-h` or `--help` prints this text and exits 0 without reading stdin.

`PermissionDenied` — fires only in auto mode, only when the classifier denied the call.
`./scripts/deploy_tools/gitops_tick.sh` is allow-listed and still denied about 1 run in 7 on
identical command text. The denial is the classifier's own variance, not a rule, so the fix is to
let the model try once more rather than to widen anything: `retry: true` tells it the call may be
reissued, and the classifier judges the reissue exactly as it judged the first. Two retries per
session cap it, so a command the classifier means to refuse still stops.

`PostToolUseFailure` — names the deploy wrapper's non-zero exit as a refusal rather than a playbook
failure, and points at the wrapper's own output for what it was. `deploy_run.py:report` prints the
name, the meaning and the remedy from `scripts/lib/exit_codes.py` on every non-zero exit, so this
hook has nothing to decode. What it still adds is the framing: a `DEPLOY_SH_NO_VERDICT` code is a
resume point, and 20 is the one where changes ARE live.

`classifierContext` is deliberately not used here. It is a PostToolUse field, and every fact
worth sending the classifier from this repo is either a failure (which lands on
PostToolUseFailure, where the field does not exist) or already stated in `autoMode.environment`
and `autoMode.allow`, where it is configuration rather than unverified application context.

Stdlib-only, like the other hooks here: it runs under `uv run` from the wrapper, and the test
suite loads it by path.
"""

import json
import os
import re
import sys

from _hook_common import read_payload, session_state_path

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

# The split, and the only part of deploy.sh's contract this hook still holds: 20 means the
# playbook RAN, every other code here means it refused first. Integers rather than a prose
# table -- the prose is `scripts/lib/exit_codes.py`'s and the wrapper prints it. The hook
# cannot import that module (stdlib-only, on the per-command hot path under `uv run
# --no-sync`), so `tests/test_auto_mode_bridge.py` holds these against it.
_REFUSALS = frozenset({2, 3, 4, 75, 76, 77, 78, 79})
_PLAYBOOK_FAILED = 20

_DEPLOY_CMD = re.compile(r"(?:^|[\s;&|])(?:\./|/[^\s]*/)?scripts/deploy\.sh(?:\s|$)")
_EXIT_CODE = re.compile(r"^Exit code (\d+)", re.MULTILINE)


def ledger_path(session_id: str) -> str:
    """Where this session's retry count lives: the per-session scratch file every hook shares."""
    return session_state_path("auto-mode-retries", session_id)


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
    """Why a failed deploy.sh is not a playbook failure, or None when this isn't one.

    Keyed on the `Exit code N` first line, which the hook docs name as the stable part of the
    error string; everything after it is display text. The meaning of the code is
    `deploy.sh`'s own to print, so this says only what an exit code cannot: where to read it,
    and that 20 is the one exit where changes are live.
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
    rc = int(match.group(1))
    if rc not in _REFUSALS and rc != _PLAYBOOK_FAILED:
        return None
    if rc == _PLAYBOOK_FAILED:
        return (
            "deploy.sh exit 20: the playbook RAN and a task failed, so this is the one deploy "
            "exit where changes ARE live -- everything applied before the failing task took "
            "effect. Read the PLAY RECAP and the failing TASK; do not assume a re-run is safe."
        )
    return (
        f"deploy.sh exit {rc} is a refusal, not a playbook failure: NOTHING was deployed, "
        "because the wrapper stopped before it applied anything. It printed the code's name, "
        "what it means and the next step on "
        "its own last two lines (`deploy.sh: <NAME> (<code>): ...` and `DEPLOY-VERDICT:`) -- "
        "read those rather than treating this as a failed deploy."
    )


def main() -> None:
    """Read the hook payload from stdin and bridge one PermissionDenied or PostToolUseFailure.

    For a PermissionDenied event, requests a retry (via `should_retry`) when the ledger
    hasn't already spent it on this session. For a PostToolUseFailure event, decodes a
    `deploy.sh` non-zero exit into an explanatory `additionalContext` note. Prints the
    corresponding hookSpecificOutput JSON and returns; any other payload is ignored.
    """
    payload = read_payload()
    if payload is None:
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
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    main()
