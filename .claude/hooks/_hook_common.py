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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))

# `segments` raises `Unsplittable` when the dotfiles package is absent; each consumer picks `ask`
# or no decision, never a silent "nothing here".
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


def command_substitutions(
    command: str, parse: Callable[[str], Any] | None = _deployed_parse
) -> list[str]:
    """The body of every `$( )`, backtick, `<( )` and `>( )` in `command`, in order.

    `split_stages` reads only the top level, so a command inside a substitution is not one
    of its stages.

    Raises:
        Unsplittable: see `segments`.
    """
    if parse is None:
        raise Unsplittable("segmenter-missing", _PARSE_UNAVAILABLE)
    parsed = parse(command)
    if not parsed.ok:
        raise Unsplittable(parsed.status)
    return list(parsed.substitutions)


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


# A stage can open with `until` or `!`, so a rule testing `stage[0]` would miss the pgrep.
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


def git_invocation(argv: list[str]):
    """`claude_guard.gitargv.invocation(argv)`: the git this argv runs, as git reads it.

    Returns None when `argv` runs no git. Imported here, beside the segmenter, so a hook
    reaches the deployed package through the one module that bootstraps it.

    Raises:
        ImportError: the `claude_guard` package is not deployed on this host.
    """
    import _claude_guard  # noqa: F401  (bootstraps claude_guard onto sys.path)
    from claude_guard.gitargv import invocation

    return invocation(argv)


def repo_root(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding a `.git` entry, or None.

    A worktree's `.git` is a file rather than a directory, so both forms count.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def tagged(hook: str, text: str) -> str:
    """`text` as hook `hook` emits it: `[<hook>] <text>`.

    Every message a hook here hands the harness opens with this prefix, so a reader can tell
    which hook wrote a deny, a context note or a failure line, and grep for it.
    `run-hook.sh` writes the same shape for the lines it emits itself.
    """
    return f"[{hook}] {text}"


def emit_pretooluse_decision(hook: str, decision: str, reason: str) -> None:
    """Print the Claude Code PreToolUse permission-decision JSON.

    Args:
        hook: the hook or arm that decided; `tagged` prefixes the reason with it.
        decision: ``"allow"``, ``"ask"`` or ``"deny"``.
        reason: the human-readable justification the harness surfaces.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": tagged(hook, reason),
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


@dataclass(frozen=True)
class HookEvent:
    """One hook call's payload, with the fields every hook reads typed once.

    A string field that is absent, empty or not a string reads as `""`, or as None where the
    attribute says so.

    Attributes:
        hook_event_name: the event, such as `PreToolUse` or `Stop`.
        session_id: the session's id.
        agent_id: the subagent's id, or None in the main session.
        agent_type: the subagent's type, or None in the main session.
        permission_mode: the session's permission mode, such as `auto`.
        cwd: the session's working directory. See `parse_payload` for a missing one.
        transcript_path: the session transcript's path.
        tool_name: the tool being called, or `""` for an event that names no tool.
        tool_input: the tool input, or `{}` when it is absent or not an object.
        raw: the whole payload, for the fields only one event carries.
    """

    hook_event_name: str
    session_id: str
    agent_id: str | None
    agent_type: str | None
    permission_mode: str
    cwd: Path
    transcript_path: str
    tool_name: str
    tool_input: Mapping[str, Any]
    raw: Mapping[str, Any]

    def bash(self) -> "BashCall | None":
        """This event as a Bash call, or None when it is not one or carries no command."""
        command = _text(self.tool_input, "command")
        if self.tool_name != "Bash" or not command:
            return None
        return BashCall(**vars(self), command=command)

    def edit(self) -> "EditCall | None":
        """This event as a file edit, or None when its tool input names no `file_path`.

        A relative `file_path` resolves against `cwd`.
        """
        file_path = _text(self.tool_input, "file_path")
        if not file_path:
            return None
        return EditCall(**vars(self), file_path=self.cwd / file_path)


@dataclass(frozen=True)
class BashCall(HookEvent):
    """A Bash tool call. A rewrite arm hands the next one `dataclasses.replace(call, command=…)`.

    Attributes:
        command: the command text, never empty. `tool_input` keeps the call's own input,
            which a rewrite leaves alone, so every other key survives into `updatedInput`.
    """

    command: str


@dataclass(frozen=True)
class EditCall(HookEvent):
    """An Edit or Write tool call.

    Attributes:
        file_path: the file the call writes, absolute.
    """

    file_path: Path


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def parse_payload(data: Mapping[str, Any]) -> HookEvent:
    """`data`, the decoded hook JSON object, as a HookEvent."""
    tool_input = data.get("tool_input")
    cwd = _text(data, "cwd")
    return HookEvent(
        hook_event_name=_text(data, "hook_event_name"),
        session_id=_text(data, "session_id"),
        agent_id=_text(data, "agent_id") or None,
        agent_type=_text(data, "agent_type") or None,
        permission_mode=_text(data, "permission_mode"),
        # DECIDED: a payload with no absolute `cwd` runs in the process cwd, not `""` or `.`.
        # Every hook joins it as a path prefix, and the shim's `cd` makes the process cwd the
        # primary checkout, so a deny that depends on the repo root reads the same root in
        # every hook.
        cwd=Path(cwd) if os.path.isabs(cwd) else Path.cwd(),
        transcript_path=_text(data, "transcript_path"),
        tool_name=_text(data, "tool_name"),
        tool_input=tool_input if isinstance(tool_input, dict) else {},
        raw=data,
    )


def read_payload(stream=None) -> HookEvent | None:
    """The hook's payload from `stream` (stdin by default), or None when there is none to judge.

    None covers empty input, text that is not JSON, a read error, and JSON that is not an
    object. Every caller treats those the same way: it decides nothing and exits 0. `stream`
    resolves `sys.stdin` at call time, because the tests swap it after this module is imported.
    """
    stream = sys.stdin if stream is None else stream
    try:
        payload = json.loads(stream.read() or "null")
    except ValueError, OSError, RecursionError:
        return None
    return parse_payload(payload) if isinstance(payload, dict) else None


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


# Test-only override, honoured only for `instructions.log` under the temp dir: the appender renames
# the file it names, so state files sharing that dir must not match.
INSTRUCTIONS_LOG_ENV = "CLAUDE_INSTRUCTIONS_LOG"
INSTRUCTIONS_LOG_MAX_BYTES = 256 * 1024
_INSTRUCTIONS_LOG_NAME = "instructions.log"


def _is_scratch_instructions_log(path: str) -> bool:
    """Whether `path` resolves to a file named `instructions.log` below the temp dir."""
    root = os.path.realpath(tempfile.gettempdir())
    resolved = os.path.realpath(path)
    return (
        os.path.basename(resolved) == _INSTRUCTIONS_LOG_NAME
        and os.path.commonpath([resolved, root]) == root
    )


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
    if override and _is_scratch_instructions_log(override):
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
    already-formatted tail (` trigger=…`, ` bytes=<n>`). One appender serves
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
