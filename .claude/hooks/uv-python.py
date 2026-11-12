#!/usr/bin/env python3
# gen-hooks: library
#   reason: the PreToolUse:Bash rewrite arm, run by bash-pretool.py in its own process
r"""The `bash-pretool.py` arm that routes bare python/pytest through `uv run`.

WHY THE REWRITE EXISTS. This repo is 3.14-only (`requires-python = ">=3.14"`) and uses PEP
758 syntax — unparenthesized `except OSError, yaml.YAMLError:` — in 8 files, among them
`scripts/diagnostics/probe.py`, `ansible/filter_plugins/toposort.py` and
`ansible/roles/k8s/monitor-bridge/files/check.py`. Ubuntu's `/usr/bin/python3` is 3.12, which
cannot *parse* those files, so a bare `pytest` reports a SyntaxError that reads like a repo bug
and is really an interpreter bug. CLAUDE.md already says to run everything through `uv run`; a
session ran the suite bare anyway, which is what this arm makes structurally impossible rather
than merely documented.

It rewrites the command instead of denying it, so the session gets the answer it asked for
rather than a round-trip. `uv run` resolves the venv from the *caller's* working directory, so a
worktree keeps its own checkout — which is why this rewrites the command rather than pinning a
PATH at the primary checkout's `.venv/bin`.

WHY IT IS AN ARM AND NO LONGER ITS OWN HOOK (#3286). It was `uv-python.sh`: 269 lines of bash
and jq, justified by "the interpreter start is the whole cost". Since #2394 `bash-pretool.py`
already puts one Python start on every Bash call, so that cost is already paid and a second
process buys nothing.

The objection the old docstring raised — a rewrite has no verdict to merge, and a rewrite arm
inside a verdict dispatcher would have to decide whether the arms after it judge the old text or
the new one — is answered by running it LAST, after the decision arms have judged the command as
typed. That is what the two separate hooks did: `bash-pretool.sh` registered at order 10 and
`uv-python.sh` at order 20, so every arm already read the text the session typed.

WHAT THE HARNESS DOES with `updatedInput` beside a `permissionDecision`, read from the 2.1.267
bundle so the fold is not a guess:

  * `deny` + a rewrite — the rewrite is dropped and the tool call keeps the text as typed. Both
    hook paths agree: the settings-hook aggregator returns at `deny` before it attaches
    `updatedInput`, and the per-hook consumer's deny branch builds its result without one. The
    command is denied either way, so there is nothing for a rewrite to apply to.
  * `ask` + a rewrite — both ride together. The consumer builds `{behavior: "ask",
    updatedInput}`, and the full permission pipeline then runs on the REWRITTEN command. So an
    `ask` from another arm still prompts, and what it prompts about is `uv run pytest`.
  * a rewrite alone — the separate `hookUpdatedInput` path, which is what this arm's own hook
    used before the fold.

That is the same answer two hooks produced, because the harness flattens every PreToolUse hook's
output into one `{deny, ask, allow, updatedInput, additionalContext}` before deciding. One hook
emitting both keys is indistinguishable from two hooks emitting one each.

FAIL-OPEN IN EVERY BRANCH, unchanged: an arm in front of every Bash call must leave the command
untouched when it cannot understand its input, so every refusal below returns None. A raise is
the same posture by construction — `bash-pretool.py` runs each arm under its own `try/except`,
so this arm raising costs the rewrite and nothing else.

`claude_guard.segment` is NOT the splitter here, where it is for every other arm. Its `Segment`
exposes no offsets into the original text and lifts heredoc bodies off the segment, so it can
say where a command begins but not where to splice. Judging needs the segments; rewriting needs
the offsets.
"""

import re

# The programs that must not run on the system interpreter. `*.py` covers a script invoked by
# its shebang (`./scripts/diagnostics/probe.py`), which names no interpreter at all and so would
# otherwise slip past every python-named pattern here. The path excludes `=`: a word like
# `P=x.py` is a shell assignment, and wrapping it made `uv run P=x.py`.
_PROGS = r"(?:python3?|pytest|py\.test|[^\s=]*\.py)"

# A program can end at the thing that ends its segment — `$(cd x; pytest)` ends at `)`, not at a
# space — so the terminator class is wider than whitespace.
_TERMINATOR = r"(?:[\s;&|)]|$)"

# Idempotent by construction, not by a check: an already-wrapped segment begins with `uv`, which
# no alternative in _PROGS matches, so it falls through untouched.
_SEGMENT_PROG = re.compile(rf"^(\s*){_PROGS}{_TERMINATOR}")

# Fast reject before any scanning. Measured on this repo's traffic the overwhelming majority of
# Bash calls name none of these, and they should pay a single regex rather than the character
# walk below.
_INTERESTING = re.compile(r"python|pytest|py\.test|\.py")

# What must follow `<<` for it to open a heredoc: an optional `-`, optional blanks, then a
# character a delimiter word can start with. It is checked before the delimiter parse because the
# two disagree about how to fail. A `<<` that names no delimiter is not a heredoc at all, and
# `_parse_heredoc_delimiter` reports that by bailing on the WHOLE command — correct for a heredoc
# it cannot read, wrong for a `<<` that was never one. Screening here lets those fall through to
# the ordinary walk instead.
_HEREDOC_OPENER = re.compile(r"^-?[ \t]*[^ \t\n;&|<>()]")

_DELIMITER_END = frozenset(";&|<>()")


def _parse_heredoc_delimiter(cmd, i):
    r"""Read the delimiter word of the heredoc whose `<<` starts at `i`.

    `<<EOF`, `<< EOF`, `<<'EOF'`, `<<"EOF"` and `<<\EOF` all name the same delimiter EOF — the
    quoting decides whether the shell expands the body, which is not something this arm reads.

    Returns:
        `(index, delimiter, strips_tabs)` where `index` is the last character of the delimiter
        word, so the walk's own increment steps past it. None when the delimiter cannot be read,
        which the caller treats as "leave the command alone".
    """
    n = len(cmd)
    j = i + 2
    strips_tabs = False
    word = []
    if j < n and cmd[j] == "-":
        strips_tabs = True
        j += 1
    while j < n and cmd[j] in " \t":
        j += 1
    while j < n:
        ch = cmd[j]
        if ch in "'\"":
            j += 1
            while j < n and cmd[j] != ch:
                word.append(cmd[j])
                j += 1
            if j >= n:
                return None
            j += 1
        elif ch == "\\":
            j += 1
            if j < n:
                word.append(cmd[j])
            j += 1
        elif ch.isspace() or ch in _DELIMITER_END:
            break
        else:
            word.append(ch)
            j += 1
    delimiter = "".join(word)
    if not delimiter:
        return None
    return j - 1, delimiter, strips_tabs


def _skip_heredoc_body(cmd, i, delimiter, strips_tabs, starts):
    """Jump from the newline that opens a heredoc body to just past its terminator line.

    The body itself contributes no command start; the position after the terminator does. A
    `<<-` heredoc lets its terminator carry leading tabs (tabs only, never spaces).

    Returns:
        The new index, or None when no terminator line exists — the same posture as an
        unterminated quote: the body's extent is a guess, so nothing is spliced.
    """
    n = len(cmd)
    pos = i + 1
    while pos < n:
        raw = cmd[pos:].split("\n", 1)[0]
        line = raw.lstrip("\t") if strips_tabs else raw
        pos = pos + len(raw) + 1
        if line == delimiter:
            if pos < n:
                starts.append(pos)
            return pos
    return None


def command_starts(cmd):
    """The offsets a command starts at, or None when the text cannot be read as shell.

    Position 0, plus every position just past a `;`, `&`, `|` or newline that the shell would
    read as a separator.

    The subtlety this walk exists for is that those same characters occur inside quoted
    arguments, where they separate nothing — `python3 -c 'a; python3 b'` must not take a `uv run`
    spliced into the middle of its -c program. A regex cannot tell the two apart, so quote state
    is tracked explicitly. Separators inside `$(...)` are left as separators on purpose: `echo
    $(cd x; pytest)` really does run pytest as a command there, so rewriting it is correct rather
    than a splice.

    A heredoc body is the other half of the same problem, and the newline is what makes it one:
    the body arrives inside the Bash tool's command text, so every line of it reads as a command
    start. A commit message reached master as `uv run health.py had grown to 938 lines`, and auto
    mode writes files through `cat > foo.py <<'EOF'`, so prose and source are both exposed. The
    body is therefore skipped opaquely — the index jumps past the terminator line rather than the
    walk continuing through it. Skipping rather than parsing is what keeps an apostrophe in
    "doesn't" from flipping the quote state, and a body that itself writes `<<EOF` from opening a
    second heredoc.
    """
    starts = [0]
    state = "none"
    delimiter = None
    strips_tabs = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if state == "none":
            if c == "'":
                state = "single"
            elif c == '"':
                state = "double"
            elif c == "\\":
                i += 1
            elif c == "<":
                if cmd[i : i + 3] == "<<<":
                    # A here-string is a single-line redirection: no body to skip.
                    i += 2
                elif cmd[i : i + 2] == "<<" and _HEREDOC_OPENER.match(cmd[i + 2 :]):
                    # Two heredocs opened on one line (`cat <<A <<B`) is rare enough that
                    # bailing beats queueing: leave the command alone.
                    if delimiter is not None:
                        return None
                    parsed = _parse_heredoc_delimiter(cmd, i)
                    if parsed is None:
                        return None
                    i, delimiter, strips_tabs = parsed
            elif c in ";&|\n":
                if c == "\n" and delimiter is not None:
                    skipped = _skip_heredoc_body(cmd, i, delimiter, strips_tabs, starts)
                    if skipped is None:
                        return None
                    i, delimiter = skipped, None
                    continue
                starts.append(i + 1)
        elif state == "single":
            # Single quotes take no escapes; only another quote ends them.
            if c == "'":
                state = "none"
        else:
            if c == "\\":
                i += 1
            elif c == '"':
                state = "none"
        i += 1
    # An unterminated quote means the walk lost track of what is quoted, so every offset after it
    # is a guess. Leave the command alone rather than splice on a guess.
    if state != "none":
        return None
    return starts


def rewrite_command(cmd):
    """`cmd` with `uv run` spliced in front of every program that needs it, or None.

    None means "leave the command alone", which covers both a command that needs nothing and
    text this arm could not read.
    """
    if not _INTERESTING.search(cmd):
        return None
    starts = command_starts(cmd)
    if starts is None:
        return None
    # Insert from the last offset backwards so the earlier ones stay valid.
    updated = cmd
    for start in reversed(starts):
        match = _SEGMENT_PROG.match(updated[start:])
        if not match:
            continue
        at = start + len(match.group(1))
        updated = updated[:at] + "uv run " + updated[at:]
    return updated if updated != cmd else None


def rewrite(payload):
    """The rewritten Bash command for `payload`, or None to leave the call as typed."""
    if payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return None
    return rewrite_command(cmd)
