#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by every hook entry point here (bash-pretool.py and its arms, inject-nested-docs.py among them, auto-mode-bridge.py, block-protected-edits.py, fanout-stop.py, log-instructions.py, session-health.py)
"""Shared helpers for the hooks in this directory.

Every hook runs standalone under the repo's uv python with the hooks dir as ``sys.path[0]`` (the
``exec uv run ... python .../X.py`` shim), and the test suite loads each hook by path from this same
dir, so a plain ``from _hook_common import ...`` resolves in both. Stdlib-only — the hooks must stay
dependency-free.
"""

import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))

# The stage splitter is the dotfiles package's segmenter. The package's `parse` cuts on the
# same separators, keeps a quoted `;` inside its word, treats a newline as a separator, and
# lifts heredoc bodies off the segment text. `_claude_guard` raises when the package is not
# deployed (its DECIDED marker refuses a stale fallback); `segments` turns that into
# `Unsplittable`, and each consumer decides what a guard that cannot read its input does — an
# `ask` for a deny guard with a real cost, no decision for the rest. Never a silent "nothing
# here".
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
    the bundled form.
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
    the tool result. Keep it under 10,000 chars — a longer one is persisted to disk and
    replaced by a preview stub.
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


def read_payload(stream=None) -> dict | None:
    """The hook's JSON payload from `stream` (stdin by default), or None when there is none to judge.

    None covers empty input, text that is not JSON, a read error, and JSON that is not an
    object. Every caller treats those the same way: it decides nothing and exits 0. `stream`
    resolves `sys.stdin` at call time, because the tests swap it after this module is imported.
    """
    stream = sys.stdin if stream is None else stream
    try:
        payload = json.loads(stream.read() or "null")
    except ValueError, OSError:
        return None
    return payload if isinstance(payload, dict) else None


def session_state_path(name: str, session_id: str) -> str:
    """The per-session scratch file for state `name`, under the system temp dir.

    The session id comes from the payload, so it is sanitised before it reaches a path: a
    `../` in it cannot leave the temp dir. The temp dir is shared between parallel sessions,
    which is why the file is keyed by session at all.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return os.path.join(tempfile.gettempdir(), f"claude-{name}-{safe}")


def gh_repo(words: list[str]) -> str | None:
    """The value of gh's `-R`/`--repo` flag in `words`, as typed, or None when there is none.

    Reads all four spellings: `--repo X`, `--repo=X`, `-R X` and the attached `-RX`. gh takes
    the last one when the flag repeats, so this does too. A GitHub URL elsewhere in the argv
    is not read: in `gh issue create --body 'see https://github.com/o/r/issues/1'` it is text,
    and only a caller that knows its subcommand takes a URL argument should look for one.
    """
    repo = None
    for i, word in enumerate(words):
        if word in ("-R", "--repo") and i + 1 < len(words):
            repo = words[i + 1]
        elif word.startswith("--repo="):
            repo = word.partition("=")[2]
        elif word.startswith("-R") and len(word) > 2:
            repo = word[2:]
    return repo


@functools.cache
def primary_checkout(start: str = HOOKS_DIR) -> str | None:
    """The primary checkout of the repo holding `start`, or None when there is none to name.

    The primary is the parent of `git rev-parse --git-common-dir`, which every linked worktree
    shares with it. A bare repo or a separated git dir has no checkout beside its git dir, so
    a common dir not named `.git` answers None, as does a `start` outside git or a git that
    fails.

    `git worktree list --porcelain`'s first entry is NOT a substitute: for a separated git dir
    it names the git dir itself, and for a bare repo the bare directory, neither of which is a
    checkout. `test_hook_common.py` holds both cases.

    Cached per `start`, because a hook process asks at most a few times and the answer cannot
    change under it. The cost is one git call, about 1.3 ms, paid only by a hook that asks.
    """
    # A hook-run git honours GIT_DIR over -C, so the caller's git env must not leak in.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                start,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=True,
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None
    if os.path.basename(common) != ".git":
        return None
    return os.path.dirname(common)


# A test points the instructions log at a scratch file through this variable rather than by
# patching a module attribute; nothing on a host sets it. It is honoured only for a path under
# the temp dir, where pytest's tmp_path lives, because the appender writes to and eventually
# renames whatever file it names: a value leaked into a real session must not reach a state
# file or another log (#3805).
INSTRUCTIONS_LOG_ENV = "CLAUDE_INSTRUCTIONS_LOG"
INSTRUCTIONS_LOG_MAX_BYTES = 256 * 1024


def _under_temp_dir(path: str) -> bool:
    """Whether `path` resolves, symlinks followed, strictly below the temp dir."""
    root = os.path.realpath(tempfile.gettempdir())
    resolved = os.path.realpath(path)
    return resolved != root and os.path.commonpath([resolved, root]) == root


def instructions_log_path(start: str = HOOKS_DIR) -> str:
    """The instructions log that `log-instructions.py` and `inject-nested-docs.py` share.

    Each session runs the hooks its own checkout carries (#3394), so a log beside the hooks
    would put a worktree session's rows in the worktree, and pruning the worktree deletes
    them. The rows grade behaviour across sessions, so every checkout writes the one log in
    the primary checkout. With no primary checkout to name, the log falls back to this
    checkout's `.claude/logs/`.

    Computed when asked, not at import: inject-nested-docs runs on every Bash call and needs
    the path only when a command names a path under a doc it might inject.
    """
    override = os.environ.get(INSTRUCTIONS_LOG_ENV)
    if override and _under_temp_dir(override):
        return override
    primary = primary_checkout(start)
    if primary is None:
        return os.path.normpath(
            os.path.join(HOOKS_DIR, "..", "logs", "instructions.log")
        )
    return os.path.join(primary, ".claude", "logs", "instructions.log")


def append_instructions_row(
    reason: str,
    mtype: str,
    fp: str,
    session_id: str,
    extra: str = "",
    start: str = HOOKS_DIR,
) -> None:
    """Append one row to the instructions log: `<ts> [<sid>] <reason> <mtype> <path><extra>`.

    `session_id` may be the full id or the 8-char prefix the row carries; `extra` is the
    already-formatted tail (` trigger=…`), empty for a session_start row. One appender serves
    both the InstructionsLoaded event and inject-nested-docs, so the log keeps one row format.
    `start` is the checkout `instructions_log_path` resolves the log from.
    """
    log = instructions_log_path(start)
    sid = (session_id or "")[:8]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "{} [{:8}] {:16} {:8} {}{}\n".format(ts, sid, reason, mtype, fp, extra)

    os.makedirs(os.path.dirname(log), exist_ok=True)
    try:  # rotate (single backup) when large
        if os.path.getsize(log) > INSTRUCTIONS_LOG_MAX_BYTES:
            os.replace(log, log + ".1")
    except OSError:
        pass
    # A single O_APPEND write below PIPE_BUF (4 KiB) is atomic across processes on
    # POSIX, so concurrent sessions can't interleave a line — no lock needed.
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8", "replace"))
    finally:
        os.close(fd)
