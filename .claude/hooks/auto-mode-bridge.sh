#!/bin/bash
# PermissionDenied(Bash) + PostToolUseFailure(Bash) hook — see auto-mode-bridge.py for what
# each event is for. Delegates to Python (exec keeps the hook's stdin JSON). No output means
# the denial or the failure is passed through unchanged, which is the safe default for both.
#
# Routed through uv so the project-pinned interpreter runs (not the system python3, which
# cannot parse this repo); --no-sync skips the env reconcile. If uv can't run, the hook emits
# nothing and both events behave as they do without it.
cd /home/ubuntu/server || exit 0
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/auto-mode-bridge.py"
