#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by bash-pretool.py, block-footguns.py, block-protected-bash.py, block-protected-edits.py, inject-nested-docs.py and nudge-land-sh.py
"""Shared helpers for the PreToolUse hooks (the bash-pretool.py arms, block-protected-edits.py).

Both hooks run standalone under the repo's uv python with the hooks dir as ``sys.path[0]`` (the
``exec uv run ... python .../X.py`` shim), and the test suite loads each hook by path from this same
dir, so a plain ``from _hook_common import ...`` resolves in both. Stdlib-only — the hooks must stay
dependency-free.
"""

import json
import shlex
from collections.abc import Callable
from typing import Any

# The stage splitter is the dotfiles package's segmenter. `block-protected-bash.py` consumed
# it first (#2053); the two hand-rolled splitters that lived here — a `;` walker in front of
# `shlex.split`, blind to newlines and heredoc bodies — went the same way in #2134. The
# package's `parse` cuts on the same separators, keeps a quoted `;` inside its word, treats a
# newline as a separator, and lifts heredoc bodies off the segment text. `_claude_guard`
# raises when the package is not deployed (its DECIDED marker refuses a stale fallback);
# `segments` turns that into `Unsplittable`, and each consumer decides what a guard that
# cannot read its input does — an `ask` for a deny guard with a real cost, no decision for
# the rest. Never a silent "nothing here".
try:
    import _claude_guard  # noqa: F401  (bootstraps claude_guard onto sys.path)
    from claude_guard.segment import parse as _parse

    _PARSE_UNAVAILABLE = ""
except ImportError as _exc:
    _parse = None
    _PARSE_UNAVAILABLE = str(_exc)


def _deployed_parse(command: str):
    """The segmenter this host has, read at call time. Raises when the package is absent."""
    if _parse is None:
        raise Unsplittable("segmenter-missing", _PARSE_UNAVAILABLE)
    return _parse(command)


class Unsplittable(Exception):
    """The command could not be read as shell, so no stage of it can be judged.

    Attributes:
        status: ``segmenter-missing`` when the ``claude_guard`` package is not deployed; the
            package's own ``unreadable:<why>`` when it refuses the text; ``unreadable:token``
            for text the package accepts but ``shlex`` cannot tokenise.
        detail: the ImportError or tokeniser message, for the reason a hook emits.
    """

    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status} ({detail})" if detail else status)
        self.status = status
        self.detail = detail

    @property
    def missing(self) -> bool:
        """True when the cause is the package not being deployed, not the command text."""
        return self.status == "segmenter-missing"


def segments(command: str, parse: Callable[[str], Any] | None = _deployed_parse):
    """The package's top-level segments of `command`, in order, heredoc bodies lifted.

    Args:
        command: the raw Bash text from the hook payload.
        parse: the segmenter. The default is whatever this host has deployed; a test hands
            `None` to stand on the undeployed host instead of patching this module.

    Raises:
        Unsplittable: the package is not deployed, or it refused the text (an unbalanced
            quote, an unclosed substitution). The package's contract is that a non-ok parse
            is a refusal, never a skip: the caller must ask or decline, not read it as
            "nothing to see".
    """
    if parse is None:
        raise Unsplittable("segmenter-missing", _PARSE_UNAVAILABLE)
    parsed = parse(command)
    if not parsed.ok:
        raise Unsplittable(parsed.status)
    return list(parsed.segments)


def split_stages(
    command: str, parse: Callable[[str], Any] | None = _deployed_parse
) -> list[list[str]]:
    """Every pipeline/sequence stage of `command`, split into argv-ish tokens.

    A hook that only inspected the first word would miss `git fetch && gh run watch`, which is
    how these calls are usually written. Each segment `segments` returns is one stage, so a
    `;`, a newline or a `|` in the text starts a new one and a `;` inside quotes does not.
    A heredoc body is not a stage: `python3 - <<EOF` yields `["python3", "-", "<<EOF"]` and
    nothing from the lines that follow.

    Raises:
        Unsplittable: see `segments`; also for a segment `shlex` cannot tokenise.
    """
    stages: list[list[str]] = []
    for segment in segments(command, parse):
        try:
            words = shlex.split(segment.text)
        except ValueError as exc:
            raise Unsplittable("unreadable:token", str(exc)) from exc
        if words:
            stages.append(words)
    return stages


# Words that can precede the real binary in a stage. `until ! pgrep -f x; do sleep 15; done`
# splits at each `;`, but its first stage still opens with `until` and `!` — a rule testing
# `stage[0]` would never see the pgrep.
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


def emit_pretooluse_context(context: str) -> None:
    """Print a PreToolUse output that adds `context` for the model and decides nothing.

    The harness accepts `additionalContext` on its own: with no `permissionDecision` the
    call proceeds through the normal permission flow and the text reaches the model beside
    the tool result (read from the 2.1.267 bundle, `jno` / `uMn`). Keep it under 10,000
    chars — a longer one is persisted to disk and replaced by a preview stub.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
