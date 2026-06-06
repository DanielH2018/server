#!/bin/bash
# PreToolUse(Bash) hook — auto-approve provably read-only commands so they don't
# prompt. Delegates to the Python classifier (exec keeps the hook's stdin JSON).
# Unrecognized commands produce no output -> normal permission flow.
exec python3 "$(dirname "$(readlink -f "$0")")/auto-approve-readonly.py"
