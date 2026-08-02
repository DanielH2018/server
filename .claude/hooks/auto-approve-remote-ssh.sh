#!/bin/bash
# PermissionRequest(Bash) hook — answer the prompt for a read-only command run over
# ssh on a homelab host, so status/inspection calls don't need a click each time.
#
# Shares the classifier with auto-approve-readonly.sh rather than re-deriving
# "read-only" in shell: the pipelines these calls are wrapped in (`… | grep | tail`,
# `2>&1`) are exactly what that parser already handles. Separate entry point because
# `Bash(ssh:*)` is an `ask` rule, and ask rules are evaluated whatever a PreToolUse
# hook returns — only a hook on this event resolves them.
#
# Same failure posture as the PreToolUse wrapper: no output -> the prompt stands.
cd /home/ubuntu/server || exit 0
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/auto-approve-readonly.py" --permission-request
