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
"""A narrow bridge between auto mode and this repo, on two events one script serves.

Usage: Claude Code runs this hook on the PostToolUseFailure:Bash and PostToolUse:Bash events,
passing `--project`; the second only for a `git push`. It reads the hook JSON payload on stdin. It
prints one `hookSpecificOutput` JSON object carrying an `additionalContext` note, or nothing, and
it always exits 0. `-h` or `--help` prints this text and exits 0 without reading stdin.

`PostToolUseFailure` and `PostToolUse` on a `git push` — GitHub prints `- Cannot update this
protected ref.` both for a ruleset that refused the push and for one the pushing account
bypassed, and names neither the ruleset nor the reason (#6327). Sessions spent two or three calls
each time working out which. On a refusal (`Repository rule violations found for refs/heads/X`, or
`push declined due to repository rule violations`) the note names `probe.py push-rules X`, which
reads the rulesets and says which rule refused and why. On a bypass notice (`Bypassed rule
violations for refs/heads/X`) the note says the push landed. The hook itself makes no network
call: it is on the per-push path, and the anonymous GitHub quota is the landing's CI wait's too.

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

_GIT_PUSH = re.compile(r"(?:^|[\s;&|(])git\s+(?:-C\s+\S+\s+)?push(?:\s|$)")
# GitHub's ruleset lines name the ref; the `[remote rejected]` line names only the branch.
_REFUSED_REF = re.compile(
    r"Repository rule violations found for refs/heads/(\S+?)[.:]?(?:\s|$)"
    r"|-> (\S+) \(push declined due to repository rule violations\)"
)
_BYPASSED_REF = re.compile(r"Bypassed rule violations for refs/heads/(\S+?):?(?:\s|$)")
_PUSH_RULES = "uv run python scripts/diagnostics/probe.py push-rules"


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

    For a PostToolUseFailure event, decodes a ruleset-refused `git push` into an explanatory
    `additionalContext` note; for a PostToolUse event, a push's bypass notice. Prints the corresponding hookSpecificOutput JSON and returns; any other
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
            note = push_refusal_note(payload)
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
