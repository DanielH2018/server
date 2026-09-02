#!/bin/bash
# PreToolUse(Bash) hook: route bare python/pytest/ansible-playbook through `uv run`.
#
# This repo is 3.14-only (`requires-python = ">=3.14"`) and uses PEP 758 syntax —
# unparenthesized `except OSError, yaml.YAMLError:` — in 8 files, among them
# scripts/diagnostics/probe.py, ansible/filter_plugins/toposort.py and
# ansible/roles/k8s/monitor-bridge/files/check.py. Ubuntu's /usr/bin/python3 is 3.12,
# which cannot *parse* those files, so a bare `pytest` reports a SyntaxError that reads
# like a repo bug and is really an interpreter bug. CLAUDE.md already says to run
# everything through `uv run`; a session ran the suite bare anyway, which is what this
# hook makes structurally impossible rather than merely documented.
#
# It rewrites the command instead of denying it, so the session gets the answer it asked
# for rather than a round-trip. `uv run` resolves the venv from the *caller's* working
# directory, so a worktree keeps its own checkout — which is why this rewrites the
# command rather than pinning a PATH at /home/ubuntu/server/.venv/bin.
#
# Fail-open in every branch: a hook in front of every Bash call must leave the command
# untouched when it cannot understand its input.

set -u

input=$(cat)

# jq, not python: this sits on the hot path of every Bash call, and the interpreter start
# is the whole cost. Same reasoning as validate-compose.sh's header.
command -v jq >/dev/null 2>&1 || exit 0

cmd=$(printf '%s' "$input" | jq -r '
    if (.tool_name // "") == "Bash" then (.tool_input.command // "") else "" end
' 2>/dev/null) || exit 0
[[ -z "$cmd" ]] && exit 0

# The ansible CLIs. They belong in PROGS for the same reason python does — the uv-tool
# shim lacks the `requests`/`docker` deps the repo pins — and they are named separately
# because the stdio fixup below keys on them too.
ANSIBLE_PROGS='ansible(-playbook|-vault|-galaxy|-console|-doc|-config|-inventory|-pull)?'

# The programs that must not run on the system interpreter. `*.py` covers a script
# invoked by its shebang (`./scripts/diagnostics/probe.py`), which names no interpreter at all and so
# would otherwise slip past every python-named pattern here.
PROGS="(python3?|pytest|py\.test|$ANSIBLE_PROGS|[^[:space:]]*\.py)"

# Every ansible CLI refuses to start on a non-blocking stdout or stderr:
# `ansible/cli/__init__.py` calls check_blocking_io() at import time and exits with
# "ERROR: Ansible requires blocking IO on stdin/stdout/stderr". Claude Code's Bash tool
# hands its child a regular file with O_NONBLOCK set (verified: st_mode 0o100660,
# O_NONBLOCK on fds 1 and 2), so every ansible run from a session died on that check while
# the same command from a terminal worked. O_NONBLOCK has no effect on writes to a regular
# file, so clearing it changes nothing about how the output is captured.
#
# Clearing the flag in the shell itself, rather than reopening the fds, because
# O_NONBLOCK lives on the open file description that fork/exec shares — one clear fixes
# the shell and every later child, and leaves a single description so nothing has to
# reason about interleaved appends. The system python3 is right here: this touches no repo
# file, so the 3.14 requirement above does not apply, and `uv run` would cost a resolve.
#
# `3>&2` is what makes the stderr half real. Without it the `2>/dev/null` that keeps this
# quiet would hand the child /dev/null as fd 2, and it would clear the flag on that
# instead. The Bash tool happens to give fds 1 and 2 one shared description today, so
# stderr came right as a side effect of fixing stdout — a detail this must not depend on,
# since it is exactly the kind of harness detail that changed here in the first place.
STDIO_FIXUP="python3 -c 'import os; [os.set_blocking(f, True) for f in (0, 1, 3)]' 3>&2 2>/dev/null; "

# Fast reject before any scanning. Measured on this repo's traffic the overwhelming
# majority of Bash calls name none of these, and they should pay a single regex rather
# than the character walk below.
[[ "$cmd" =~ (python|pytest|py\.test|ansible|\.py) ]] || exit 0

# Find the offsets a command starts at: position 0, and every position just past a `;`,
# `&`, `|` or newline that the shell would read as a separator.
#
# The subtlety this walk exists for is that those same characters occur inside quoted
# arguments, where they separate nothing — `python3 -c 'a; python3 b'` must not take a
# `uv run` spliced into the middle of its -c program. A regex cannot tell the two apart,
# so quote state is tracked explicitly. Separators inside `$(...)` are left as
# separators on purpose: `echo $(cd x; pytest)` really does run pytest as a command
# there, so rewriting it is correct rather than a splice.
declare -a starts=(0)
state=none
i=0
n=${#cmd}
while ((i < n)); do
    c=${cmd:i:1}
    case "$state" in
        none)
            case "$c" in
                "'") state=single ;;
                '"') state=double ;;
                "\\") ((i++)) ;;
                ';' | '&' | '|' | $'\n') starts+=("$((i + 1))") ;;
            esac
            ;;
        single)
            # Single quotes take no escapes; only another quote ends them.
            [[ "$c" == "'" ]] && state=none
            ;;
        double)
            if [[ "$c" == "\\" ]]; then
                ((i++))
            elif [[ "$c" == '"' ]]; then
                state=none
            fi
            ;;
    esac
    ((i++))
done

# An unterminated quote means the walk lost track of what is quoted, so every offset
# after it is a guess. Leave the command alone rather than splice on a guess.
[[ "$state" == none ]] || exit 0

# Insert from the last offset backwards so the earlier ones stay valid.
updated=$cmd
for ((k = ${#starts[@]} - 1; k >= 0; k--)); do
    p=${starts[k]}
    rest=${updated:p}
    # Idempotent by construction, not by a check: an already-wrapped segment begins with
    # `uv`, which no alternative in $PROGS matches, so it falls through untouched.
    # The terminator class is wider than whitespace so a program can end at the thing that
    # ends its segment — `$(cd x; pytest)` ends at `)`, not at a space.
    [[ "$rest" =~ ^([[:space:]]*)$PROGS([[:space:];\&|\)]|$) ]] || continue
    ins=$((p + ${#BASH_REMATCH[1]}))
    updated="${updated:0:ins}uv run ${updated:ins}"
done

# The fixup goes in front of the whole command rather than each segment: the flag it
# clears is shared, so one clear at the front covers everything after it.
#
# Only the ansible CLIs get it, rather than everything this hook routes. The fixup is
# harmless anywhere, but it changes the command text the auto-mode classifier reads, and
# an ansible command already prompts where a `pytest` does not. A script that spawns
# ansible without naming it — `secret_rotation.py rotate --deploy` — restores the flag
# itself instead; nothing in its command text could have told this hook to.
#
# Matched against $cmd, whose offsets the walk computed and the splices above invalidated,
# and past an optional `uv run` with its own flags, so it fires whether or not the rewrite
# added one and whether or not the session typed `--frozen`.
for p in "${starts[@]}"; do
    rest=${cmd:p}
    if [[ "$rest" =~ ^[[:space:]]*(uv[[:space:]]+run[[:space:]]+(--[^[:space:]]+[[:space:]]+)*)?$ANSIBLE_PROGS([[:space:];\&|\)]|$) ]]; then
        [[ "$updated" == *"os.set_blocking"* ]] || updated="$STDIO_FIXUP$updated"
        break
    fi
done

[[ "$updated" == "$cmd" ]] && exit 0

jq -n --arg c "$updated" '{
    hookSpecificOutput: {
        hookEventName: "PreToolUse",
        updatedInput: {command: $c}
    }
}'
