#!/bin/bash
# PreToolUse(Bash) hook — deny hand-polled CI in favour of land.sh. Routed through uv so the
# project-pinned interpreter runs (not the system python3); --no-sync skips the env reconcile
# to stay fast on the per-command hot path. `exec` preserves the hook's stdin JSON. No output
# -> normal permission flow.
cd /home/ubuntu/server || exit 0
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/nudge-land-sh.py"
