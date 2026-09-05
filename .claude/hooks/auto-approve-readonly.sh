#!/bin/bash
# PreToolUse(Bash) hook — auto-approve provably read-only commands so they don't
# prompt. Delegates to the Python classifier (exec keeps the hook's stdin JSON).
# Unrecognized commands produce no output -> normal permission flow.
#
# Routed through uv so the project-pinned interpreter runs (not the system
# python3); --no-sync skips the env reconcile to stay fast on the per-Bash hot
# path. If uv can't run, the hook simply emits nothing -> normal prompt (safe).
cd /home/ubuntu/server || { echo "auto-approve-readonly.sh: classifier did not run (cd to /home/ubuntu/server failed) — command falls through to the normal permission flow" >&2; exit 0; }
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/auto-approve-readonly.py"
