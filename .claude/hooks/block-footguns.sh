#!/bin/bash
# PreToolUse(Bash) hook — deny four commands that fail silently on this machine (ugrep's -Z/-z,
# a bare `git stash pop`, a Forbidden `kubectl rollout restart`, remote git with no `cd`).
# Routed through uv so the project-pinned interpreter runs (not the system python3); --no-sync
# skips the env reconcile to stay fast on the per-command hot path. `exec` preserves the hook's
# stdin JSON. No output -> normal permission flow.
# DECIDED: fail-open, not fail-closed, on all three ways this shim can break — a broken
# guard must not brick every tool call. `uv` missing or the .py missing already write their
# own line to stderr; only the `cd` arm was silent, so it now matches them. See issue #1014.
cd /home/ubuntu/server || { echo "block-footguns.sh: guard did not run (cd to /home/ubuntu/server failed) — command allowed through" >&2; exit 0; }
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/block-footguns.py"
