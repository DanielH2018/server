#!/usr/bin/env python3
# gen-hooks: register
#   event: PostToolUseFailure
#   matcher: Bash
#   timeout: 10
#   order: 10
#   args: --project
# gen-hooks: register
#   event: PostToolUse
#   matcher: Bash
#   timeout: 10
#   order: 10
#   args: --project
#   if: Bash(git push*)
"""Narrow bridges between auto mode and this repo, on two events one script serves.

Usage: Claude Code runs this hook on the PostToolUseFailure:Bash and PostToolUse:Bash events,
passing `--project`; the second only for a `git push`. It reads the hook JSON payload on stdin. It
prints one `hookSpecificOutput` JSON object carrying an `additionalContext` note, or nothing, and
it always exits 0. `-h` or `--help` prints this text and exits 0 without reading stdin.

`PostToolUseFailure` — names the deploy wrapper's non-zero exit as a refusal rather than a playbook
failure, and points at the wrapper's own output for what it was. `deploy_lib/run.py:report` prints the
name, the meaning and the remedy from `scripts/lib/exit_codes.py` on every non-zero exit, so this
hook has nothing to decode. What it still adds is the framing: a `DEPLOY_SH_NO_VERDICT` code is a
resume point, and 20 is the one where changes ARE live.

`PostToolUseFailure` and `PostToolUse` on a `git push` — GitHub prints `- Cannot update this
protected ref.` both for a ruleset that refused the push and for one the pushing account
bypassed, and names neither the ruleset nor the reason (#6327). Sessions spent two or three calls
each time working out which. On a refusal (`Repository rule violations found for refs/heads/X`, or
`push declined due to repository rule violations`) the note names `probe.py push-rules X`, which
reads the rulesets and says which rule refused and why. On a bypass notice (`Bypassed rule
violations for refs/heads/X`) the note says the push landed. The hook itself makes no network
call: it is on the per-push path, and the anonymous GitHub quota is the deployer's CI gate's too.

`classifierContext` is deliberately not used here. It is a PostToolUse field, and every fact
worth sending the classifier from this repo is either a failure (which lands on
PostToolUseFailure, where the field does not exist) or already stated in `autoMode.environment`
and `autoMode.allow`, where it is configuration rather than unverified application context.

Stdlib-only, like the other hooks here: it runs under `uv run` from the wrapper, and the test
suite loads it by path.
"""

import json
import re
import sys

from _hook_common import read_payload

# The split, and the only part of deploy.sh's contract this hook still holds: 20 means the
# playbook RAN, every other code here means it refused first. Integers rather than a prose
# table -- the prose is `scripts/lib/exit_codes.py`'s and the wrapper prints it. The hook
# cannot import that module (stdlib-only, on the per-command hot path under `uv run
# --no-sync`), so `tests/test_auto_mode_bridge.py` holds these against it.
_REFUSALS = frozenset({2, 3, 4, 5, 75, 76, 77, 78, 79})
_PLAYBOOK_FAILED = 20

_GIT_PUSH = re.compile(r"(?:^|[\s;&|(])git\s+(?:-C\s+\S+\s+)?push(?:\s|$)")
# GitHub's ruleset lines name the ref; the `[remote rejected]` line names only the branch.
_REFUSED_REF = re.compile(
    r"Repository rule violations found for refs/heads/(\S+?)[.:]?(?:\s|$)"
    r"|-> (\S+) \(push declined due to repository rule violations\)"
)
_BYPASSED_REF = re.compile(r"Bypassed rule violations for refs/heads/(\S+?):?(?:\s|$)")
_PUSH_RULES = "uv run python scripts/diagnostics/probe.py push-rules"

_DEPLOY_CMD = re.compile(r"(?:^|[\s;&|])(?:\./|/[^\s]*/)?scripts/deploy\.sh(?:\s|$)")
_EXIT_CODE = re.compile(r"^Exit code (\d+)", re.MULTILINE)


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


def _branches(pattern: re.Pattern, text: str) -> list[str]:
    """Every distinct branch `pattern` captures in `text`, in order of appearance."""
    found = (next(g for g in m.groups() if g) for m in pattern.finditer(text))
    return list(dict.fromkeys(found))


def push_refusal_note(payload: dict) -> str | None:
    """Which command names the ruleset behind a refused `git push`, or None for any other failure."""
    if payload.get("tool_name") != "Bash" or payload.get("is_interrupt"):
        return None
    if not _GIT_PUSH.search(payload.get("tool_input", {}).get("command", "")):
        return None
    branches = _branches(_REFUSED_REF, payload.get("error", "") or "")
    if not branches:
        return None
    commands = "; ".join(f"`{_PUSH_RULES} {b}`" for b in branches)
    return (
        "GitHub refused this push with a repository ruleset, and its message names neither the "
        f"ruleset nor the reason. To see which rule refused it and why, run {commands}: it reads "
        "the rules matching the branch, evaluates them against the push (unsigned commits, a "
        "force-push, a fenced branch, a direct push to master), and says whether your gh login "
        "bypasses each."
    )


def push_bypass_note(payload: dict) -> str | None:
    """Why a `git push` that printed a bypass notice still landed, or None for any other push."""
    if payload.get("tool_name") != "Bash":
        return None
    if not _GIT_PUSH.search(payload.get("tool_input", {}).get("command", "")):
        return None
    response = payload.get("tool_response")
    if isinstance(response, dict):
        text = f"{response.get('stdout', '')}\n{response.get('stderr', '')}"
    else:
        text = str(response or "")
    branches = _branches(_BYPASSED_REF, text)
    if not branches:
        return None
    return (
        f"`Bypassed rule violations for refs/heads/{branches[0]}` and the `- Cannot update this "
        "protected ref.` under it are a NOTICE, not a refusal: the pushing account bypasses that "
        "ruleset, and the push landed. A refusal reads `Repository rule violations found` and "
        "`[remote rejected]`. Do not spend calls re-verifying; "
        f"`{_PUSH_RULES} {branches[0]}` names the ruleset if you need it."
    )


def main() -> None:
    """Read the hook payload from stdin and bridge one PostToolUseFailure or PostToolUse.

    For a PostToolUseFailure event, decodes a `deploy.sh` non-zero exit or a ruleset-refused
    `git push` into an explanatory `additionalContext` note; for a PostToolUse event, a push's
    bypass notice. Prints the corresponding hookSpecificOutput JSON and returns; any other
    payload is ignored.
    """
    payload = read_payload()
    if payload is None:
        return

    event = payload.get("hook_event_name")
    if event in ("PostToolUseFailure", "PostToolUse"):
        if event == "PostToolUse":
            note = push_bypass_note(payload)
        else:
            note = deploy_exit_note(payload) or push_refusal_note(payload)
        if note:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": event,
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
