#!/bin/bash
# PreToolUse(Bash) hook — deny four commands that fail silently on this machine (ugrep's -Z/-z,
# a bare `git stash pop`, a Forbidden `kubectl rollout restart`, remote git with no `cd`).
# Routed through uv so the project-pinned interpreter runs (not the system python3); --no-sync
# skips the env reconcile to stay fast on the per-command hot path. `exec` preserves the hook's
# stdin JSON. No output -> normal permission flow.
cd /home/ubuntu/server || exit 0
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/block-footguns.py"
