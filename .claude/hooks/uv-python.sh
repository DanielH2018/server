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

# The programs that must not run on the system interpreter. `*.py` covers a script
# invoked by its shebang (`./scripts/diagnostics/probe.py`), which names no interpreter at all and so
# would otherwise slip past every python-named pattern here.
PROGS='(python3?|pytest|py\.test|ansible-playbook|[^[:space:]]*\.py)'

# Fast reject before any scanning. Measured on this repo's traffic the overwhelming
# majority of Bash calls name none of these, and they should pay a single regex rather
# than the character walk below.
[[ "$cmd" =~ (python|pytest|py\.test|ansible-playbook|\.py) ]] || exit 0

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

[[ "$updated" == "$cmd" ]] && exit 0

jq -n --arg c "$updated" '{
    hookSpecificOutput: {
        hookEventName: "PreToolUse",
        updatedInput: {command: $c}
    }
}'
