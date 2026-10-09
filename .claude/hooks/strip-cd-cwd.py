#!/usr/bin/env python3
# gen-hooks: library
#   reason: the PreToolUse:Bash arm that drops a leading cd into the session's own cwd, run by bash-pretool.py
"""The `bash-pretool.py` arm that drops a leading `cd <dir> &&` when `<dir>` is the session's cwd.

WHY. The auto-mode classifier refuses a compound command that it would pass as a single one,
and a leading `cd` makes any command compound. In 70 fan-out transcripts, 727 implementer
commands began with `cd <own worktree> &&` although the session already ran there, and 160
turns ended on a "contains multiple operations" or "requested permissions" refusal (#3957).
A `cd` into the directory the shell is already in changes nothing the command does, so
dropping it leaves the command identical in effect and single in shape.

THE CWD IS THE PAYLOAD'S, never this process's. `run-hook.sh` changes into the primary
checkout to find its `.venv`, so `os.getcwd()` here is `/home/ubuntu/server` for every
worktree session. Comparing against it would strip `cd /home/ubuntu/server && x` inside a
worktree, which moves the command into another checkout.

THE MATCH IS NARROW ON PURPOSE. Only a leading `cd <word> &&` is stripped, where the word is a
bare token or a quoted string with no expansion in it. A word with `$` or a backtick names a
directory only the shell can resolve; `cd -`, `cd -P` and a bare `cd` name one this arm would
have to guess; and `cd x;` or `cd x ||` run the rest whether or not the `cd` succeeded, which a
strip would not change but which nobody has measured as a cost. Anything else returns None and
the command stands, the same fail-open posture as `uv-python.py`.

It runs BEFORE `uv-python`, which then routes the stripped text, so `cd <cwd> && pytest` becomes
`uv run pytest`. The decision arms still judge the command as typed: a `cd` into the cwd changes
no path they resolve, so the typed and the stripped command get the same verdict.
"""

import os
import re

# One leading `cd <word> &&`. The word alternatives are a single-quoted string, a double-quoted
# string without `$`, a backtick or a backslash, and a bare token without shell metacharacters.
# A bare token may not start with `-`, which keeps `cd -`, `cd -P` and `cd --` out.
_LEADING_CD = re.compile(
    r"""^\s*cd[ \t]+
    (?:'(?P<single>[^']*)'
      |"(?P<double>[^"$`\\]*)"
      |(?P<bare>[^\s'"$`\\;&|<>()\-][^\s'"$`\\;&|<>()]*))
    [ \t]*&&\s*""",
    re.VERBOSE,
)


def _target(match):
    for group in ("single", "double", "bare"):
        if match.group(group) is not None:
            return match.group(group)
    return None


def _is_cwd(word, cwd):
    """Whether `word`, read as a `cd` argument from `cwd`, resolves to `cwd` itself."""
    if not word:
        return False
    path = os.path.expanduser(word)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path) == os.path.realpath(cwd)


def strip_command(cmd, cwd):
    """`cmd` without its leading `cd <cwd> &&` prefixes, or None when there is none to drop.

    Repeated prefixes are all dropped, since each is the same no-op. A command that would be
    left empty is returned as None, because `cd <cwd> &&` with nothing after it is not a
    command the shell accepts and so not one to rewrite.
    """
    if not cwd or not os.path.isabs(cwd):
        return None
    updated = cmd
    while True:
        match = _LEADING_CD.match(updated)
        if not match or not _is_cwd(_target(match), cwd):
            break
        rest = updated[match.end() :]
        if not rest.strip():
            break
        updated = rest
    return updated if updated != cmd else None


def rewrite(payload):
    """The Bash command for `payload` without a leading `cd` into its cwd, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or ""
    if not cmd or not isinstance(cwd, str):
        return None
    return strip_command(cmd, cwd)
