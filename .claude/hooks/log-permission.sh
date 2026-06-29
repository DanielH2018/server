#!/bin/bash
# PreToolUse / PermissionRequest / Notification hook — observability only. Records
# every in-scope tool call + permission prompt into .claude/logs/permissions.json
# so the allowlist can be tuned with data (see scripts/audit-permissions.py and the
# /audit-permissions skill). Wired `async` in settings.json so it never blocks a
# tool call; the Python swallows all errors too.
#
# Routed through uv so the project-pinned interpreter runs (not the system
# python3); --no-sync skips the env reconcile to stay fast on the per-call hot
# path. If uv can't run, the hook simply records nothing -> no effect (safe).
cd /home/ubuntu/server || exit 0
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/log-permission.py"
