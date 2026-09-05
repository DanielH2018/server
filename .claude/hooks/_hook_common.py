#!/usr/bin/env python3
"""Shared helpers for the PreToolUse hooks (auto-approve-readonly.py, block-protected-edits.py).

Both hooks run standalone under the repo's uv python with the hooks dir as ``sys.path[0]`` (the
``exec uv run ... python .../X.py`` shim), and the test suite loads each hook by path from this same
dir, so a plain ``from _hook_common import ...`` resolves in both. Stdlib-only — the hooks must stay
dependency-free.
"""

import json
import shlex


def _split_top_level_semicolons(command: str) -> list[str]:
    """`command` cut at every `;` that sits outside quoting, each piece still raw shell text.

    `shlex.split` leaves an unquoted `;` glued to the word before it (`"hi;"`), so no token
    it returns is ever exactly `";"` — every rule keyed on that separator is unreachable. This
    walks the raw string instead, tracking quote/escape state one character at a time, so a
    `;` inside `'...'`/`"..."` or after a backslash stays part of its piece while a bare one
    becomes a cut point. `;;` and `;&` (case-statement terminators) collapse into the same cut
    as a lone `;` — swallowing the second character rather than leaving a stray `&`/`;` glued
    to the next piece, which would just relocate the same bypass one character over.
    """
    pieces: list[str] = []
    current: list[str] = []
    in_single = in_double = escaped = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            current.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if ch == ";" and not in_single and not in_double:
            pieces.append("".join(current))
            current = []
            i += 1
            if i < n and command[i] in (";", "&"):
                i += 1
            continue
        current.append(ch)
        i += 1
    pieces.append("".join(current))
    return pieces


def split_stages(command: str) -> list[list[str]]:
    """Every pipeline/sequence stage of `command`, split into argv-ish tokens.

    A hook that only inspected the first word would miss `git fetch && gh run watch`, which is
    how these calls are usually written. Unbalanced quotes return no stages: there is nothing
    reliable to match on, and a hook that guesses at a command it cannot parse is worse than
    one that declines to judge it.

    `;` is cut out before `shlex.split` ever sees it (see `_split_top_level_semicolons`), so
    each semicolon-delimited piece is tokenised and flushed as its own stage boundary here,
    the same as hitting a literal `&&`/`||`/`|`/`&` token below.
    """
    stages: list[list[str]] = []
    current: list[str] = []
    for piece in _split_top_level_semicolons(command):
        try:
            words = shlex.split(piece)
        except ValueError:
            return []
        for word in words:
            if word in ("&&", "||", "|", "&"):
                if current:
                    stages.append(current)
                current = []
            else:
                current.append(word)
        if current:
            stages.append(current)
        current = []
    return stages


# Words that can precede the real binary in a stage. `shlex.split` leaves `;` attached to the
# word before it, so `until ! pgrep -f x; do sleep 15; done` arrives as ONE stage whose first
# word is `until` — a rule testing `stage[0]` would never see the pgrep.
_LEADING_KEYWORDS = frozenset(
    {
        "!",
        "until",
        "while",
        "if",
        "elif",
        "then",
        "do",
        "time",
        "command",
        "then;",
        "do;",
    }
)


def strip_shell_keywords(stage: list[str]) -> list[str]:
    """`stage` with any leading shell keywords and negations removed.

    Use this before testing `stage[0]`, so a rule catches the command inside a loop or an `if`
    as well as the bare form. It only strips from the FRONT: a later `do`/`then` belongs to the
    loop body, and dropping those would splice unrelated words onto the command being judged.
    """
    i = 0
    while i < len(stage) and stage[i] in _LEADING_KEYWORDS:
        i += 1
    return stage[i:]


def invokes(stage: list[str], prefix: tuple[str, ...]) -> bool:
    """True when `stage` invokes `prefix`, allowing global flags before the subcommand.

    `gh run watch` and `gh --repo o/r run watch` are the same command. Dropping every flag
    instead would drop a flag's VALUE with it (`--repo o/r` leaves a bare `o/r` that shifts
    every position), so the subcommand words are matched as an adjacent run anywhere after the
    binary — which also keeps `gh issue list --search run --label watch` from matching.
    """
    if not stage or stage[0] != prefix[0]:
        return False
    words, rest = list(prefix[1:]), stage[1:]
    if not words:
        return True
    return any(rest[i : i + len(words)] == words for i in range(len(rest)))


def short_flags(stage: list[str]) -> set[str]:
    """Every single-letter flag in `stage`, unbundled.

    `-lZ` is `-l` and `-Z`, and a hook that only compared whole arguments to `-Z` would miss
    the bundled form — which is the form the ugrep incident was actually written in.
    """
    letters: set[str] = set()
    for word in stage:
        if word.startswith("-") and not word.startswith("--") and len(word) > 1:
            letters.update(word[1:])
    return letters


def emit_permissionrequest_allow() -> None:
    """Print the Claude Code PermissionRequest decision that answers a pending prompt.

    Separate from the PreToolUse decision below because the two events sit at different points
    in the permission pipeline. Claude Code evaluates ``ask`` rules regardless of what a
    PreToolUse hook returns, so a command covered by one (``Bash(ssh:*)``) is only ever
    resolved by a hook on this event, which runs as part of that prompt.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
        )
    )


def emit_pretooluse_decision(decision: str, reason: str) -> None:
    """Print the Claude Code PreToolUse permission-decision JSON.

    ``decision`` is ``"allow"`` or ``"deny"``; ``reason`` is the human-readable
    justification the harness surfaces.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
