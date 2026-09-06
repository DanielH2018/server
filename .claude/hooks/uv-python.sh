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
# reason about interleaved appends.
#
# `stdio-blocking` (the dotfiles repo, `home/dot_local/bin/executable_stdio-blocking`) is
# the same fix as a standalone binary, added so this prefix stops matching the
# `Bash(python3 -c:*)` ask rule there — that rule is right for arbitrary `python3 -c`, but
# this fixup runs ahead of EVERY ansible invocation, so the ask rule prompted for the
# whole compound chain regardless of what followed it. Preferred when it is on PATH.
# The inline `python3 -c` form is the fallback for a machine the dotfiles have not
# deployed to yet: `3>&2` routes the real stderr through fd 3 so `2>/dev/null` can quiet
# the fixup's own output without clearing the flag on /dev/null instead.
if command -v stdio-blocking >/dev/null 2>&1; then
    STDIO_FIXUP="stdio-blocking; "
else
    STDIO_FIXUP="python3 -c 'import os; [os.set_blocking(f, True) for f in (0, 1, 3)]' 3>&2 2>/dev/null; "
fi

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
#
# A heredoc body is the other half of the same problem, and the newline is what makes it
# one: the body arrives inside the Bash tool's command text, so every line of it read as a
# command start. A commit message reached master as `uv run health.py had grown to 938
# lines`, and auto mode writes files through `cat > foo.py <<'EOF'`, so prose and source
# are both exposed. The body is therefore skipped opaquely — `i` jumps past the terminator
# line rather than the walk continuing through it. Skipping rather than parsing is what
# keeps an apostrophe in "doesn't" from flipping the quote state, and a body that itself
# writes `<<EOF` from opening a second heredoc.

# What must follow `<<` for it to open a heredoc: an optional `-`, optional blanks, then a
# character a delimiter word can start with. It is checked before the delimiter parse
# because the two disagree about how to fail. A `<<` that names no delimiter is not a
# heredoc at all, and `parse_heredoc_delimiter` reports that by bailing on the WHOLE command
# — correct for a heredoc it cannot read, wrong for a `<<` that was never one. Screening
# here lets those fall through to the ordinary walk instead.
HEREDOC_OPENER=$'^-?[ \t]*[^ \t\n;&|<>()]'

# Read the delimiter word of a heredoc whose `<<` starts at $i, and leave $i on its last
# character so the walk's own increment steps past it. `<<EOF`, `<< EOF`, `<<'EOF'`,
# `<<"EOF"` and `<<\EOF` all name the same delimiter EOF — the quoting decides whether the
# shell expands the body, which is not something this hook reads. Returns non-zero when the
# delimiter cannot be read, which the caller treats as "leave the command alone".
parse_heredoc_delimiter() {
    local j=$((i + 2)) ch q
    heredoc_dash=
    heredoc_delim=
    [[ ${cmd:j:1} == '-' ]] && heredoc_dash=1 && ((j++))
    while [[ ${cmd:j:1} == ' ' || ${cmd:j:1} == $'\t' ]]; do ((j++)); done
    while ((j < n)); do
        ch=${cmd:j:1}
        case "$ch" in
            "'" | '"')
                q=$ch
                ((j++))
                while ((j < n)) && [[ ${cmd:j:1} != "$q" ]]; do
                    heredoc_delim+=${cmd:j:1}
                    ((j++))
                done
                ((j < n)) || return 1
                ((j++))
                ;;
            "\\")
                ((j++))
                heredoc_delim+=${cmd:j:1}
                ((j++))
                ;;
            [[:space:]] | ';' | '&' | '|' | '<' | '>' | '(' | ')')
                break
                ;;
            *)
                heredoc_delim+=$ch
                ((j++))
                ;;
        esac
    done
    [[ -n "$heredoc_delim" ]] || return 1
    i=$((j - 1))
}

# Jump $i from the newline that opens a heredoc body to just past its terminator line, and
# register that position as a command start — the body itself contributes none. A `<<-`
# heredoc lets its terminator carry leading tabs (tabs only, never spaces). Returns
# non-zero when no terminator line exists, the same posture as an unterminated quote: the
# body's extent is a guess, so nothing is spliced.
skip_heredoc_body() {
    local pos=$((i + 1)) line
    while ((pos < n)); do
        line=${cmd:pos}
        line=${line%%$'\n'*}
        local raw=$line
        if [[ -n "$heredoc_dash" ]]; then
            while [[ "$line" == $'\t'* ]]; do line=${line#$'\t'}; done
        fi
        pos=$((pos + ${#raw} + 1))
        if [[ "$line" == "$heredoc_delim" ]]; then
            heredoc_delim=
            ((pos < n)) && starts+=("$pos")
            i=$pos
            return 0
        fi
    done
    return 1
}

declare -a starts=(0)
state=none
heredoc_delim=
heredoc_dash=
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
                '<')
                    if [[ ${cmd:i:3} == '<<<' ]]; then
                        # A here-string is a single-line redirection: no body to skip.
                        ((i += 2))
                    elif [[ ${cmd:i:2} == '<<' && ${cmd:i+2} =~ $HEREDOC_OPENER ]]; then
                        # Two heredocs opened on one line (`cat <<A <<B`) is rare enough
                        # that bailing beats queueing: leave the command alone.
                        [[ -z "$heredoc_delim" ]] && parse_heredoc_delimiter || exit 0
                    fi
                    ;;
                ';' | '&' | '|' | $'\n')
                    if [[ "$c" == $'\n' && -n "$heredoc_delim" ]]; then
                        skip_heredoc_body || exit 0
                        continue
                    fi
                    starts+=("$((i + 1))")
                    ;;
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
        [[ "$updated" == *"os.set_blocking"* || "$updated" == *"stdio-blocking"* ]] || updated="$STDIO_FIXUP$updated"
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
