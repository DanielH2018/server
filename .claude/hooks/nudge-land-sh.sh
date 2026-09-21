#!/bin/bash
# gen-hooks: register
#   event: PreToolUse
#   matcher: Bash
#   timeout: 10
#   order: 40
# PreToolUse(Bash) hook — deny hand-polled CI in favour of land.sh. Routed through uv so the
# project-pinned interpreter runs (not the system python3); --no-sync skips the env reconcile
# to stay fast on the per-command hot path. `exec` preserves the hook's stdin JSON. No output
# -> normal permission flow.
# DECIDED: the `cd` arm asks; the other two arms stay fail-open. Issue #1014 made all three
# fail-open so that a broken guard does not brick every tool call, and gave the `cd` arm the
# stderr line the other two already had. An `ask` is a prompt, not a brick: the operator reads
# why a DENY guard could not run and decides once, where a bare `exit 0` disarmed it in
# silence (#2171). `uv` missing or the .py missing still exit non-zero with their own stderr
# line — a failed `exec` ends a non-interactive shell, so no `ask` can follow it.
cd /home/ubuntu/server || {
  echo "nudge-land-sh.sh: guard did not run (cd to /home/ubuntu/server failed) — asking instead of allowing" >&2
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"nudge-land-sh.sh: guard did not run (cd to /home/ubuntu/server failed), so this call was not checked. Review it yourself."}}'
  exit 0
}
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/nudge-land-sh.py"
