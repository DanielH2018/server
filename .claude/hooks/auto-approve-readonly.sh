#!/bin/bash
CMD=$(jq -r '.tool_input.command' < /dev/stdin)
if echo "$CMD" | grep -qE '^(ls|cat|echo|pwd|whoami|date|git status|git log|git diff)'; then
  echo '{"permissionDecision": "allow", "permissionDecisionReason": "Safe read-only command"}'
fi
