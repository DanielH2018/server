#!/bin/bash
# PreToolUse(Bash) hook: route bare python/pytest/ansible-playbook through `uv run`.
#
# This repo is 3.14-only (`requires-python = ">=3.14"`) and uses PEP 758 syntax —
# unparenthesized `except OSError, yaml.YAMLError:` — in 8 files, among them
# scripts/probe.py, ansible/filter_plugins/toposort.py and
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
# invoked by its shebang (`./scripts/probe.py`), which names no interpreter at all and so
# would otherwise slip past every python-named pattern here.
PROGS='(python3?|pytest|py\.test|ansible-playbook|[^[:space:]]*\.py)'

# Two rewrite scopes, and the narrow one exists purely to stay out of quoted text.
#
# A segment-start rewrite has to split on `;`, `&&`, `||` and `|`, and those characters
# also occur *inside* quoted arguments — `python3 -c 'a; python3 b'` would take a
# `uv run` spliced into the middle of the -c program. So when the command contains a
# quote, a backtick or a `$`, only position 0 is rewritten: the leading program of a
# command is unambiguous no matter what follows it.
if [[ "$cmd" == *[\'\"\`\$]* ]]; then
    updated=$(printf '%s' "$cmd" | sed -E "1s@^([[:space:]]*)$PROGS([[:space:]]|\$)@\1uv run \2\3@")
else
    updated=$(printf '%s' "$cmd" | sed -E "s@(^|[;&|])([[:space:]]*)$PROGS([[:space:]]|\$)@\1\2uv run \3\4@g")
fi

# Idempotent by construction, not by a check: `uv run pytest` has `uv` as its segment's
# first word, so no pattern here matches it. An already-wrapped command falls through as
# unchanged and this exits silently.
[[ "$updated" == "$cmd" ]] && exit 0

jq -n --arg c "$updated" '{
    hookSpecificOutput: {
        hookEventName: "PreToolUse",
        updatedInput: {command: $c}
    }
}'
