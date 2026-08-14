#!/bin/bash
# PostToolUse hook: run ansible-lint after editing Ansible YAML files

# Read hook input from stdin
input=$(cat)

cd /home/ubuntu/server || exit 0

# Extract the file path from tool input. This used to spin up `uv run --no-sync python`
# to read one JSON field, which cost 39ms of the hook's 42ms on an edit this hook then
# declined to lint -- and it ran on EVERY Edit and Write, ~2,200 of them in the week of
# 2026-08-07. jq does the same job in 2ms and is already a hard dependency of the
# PermissionRequest hooks. Same failure posture: unreadable input means no path, and the
# guard below exits without linting.
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")

# Only proceed if we have a file path
if [[ -z "$file_path" ]]; then
    exit 0
fi

# Only lint YAML files inside ansible/ (skip encrypted vars/secrets dirs)
if [[ "$file_path" == *"/ansible/"* ]] && [[ "$file_path" == *.yml || "$file_path" == *.yaml ]]; then
    if [[ "$file_path" == *"/vars/"* || "$file_path" == *"/secrets/"* ]]; then
        exit 0
    fi

    echo "ansible-lint: checking $(basename "$file_path")..."
    # Lint from the checkout that owns the file, not always the primary one: a file in a
    # .claude/worktrees/<name>/ checkout linted from /home/ubuntu/server gets a path
    # ansible-lint can't resolve to a role, and every role variable is then reported as
    # a var-naming[no-role-prefix] violation.
    repo_root="${file_path%%/ansible/*}"
    cd "$repo_root" || exit 0
    relative_path="${file_path#"$repo_root"/}"
    # Worktrees don't carry the vendored collections (gitignored, installed once in the
    # primary checkout), so point ansible-lint's module resolution back at those.
    export ANSIBLE_COLLECTIONS_PATH=/home/ubuntu/server/ansible/collections
    /home/ubuntu/.local/bin/ansible-lint "$relative_path" 2>&1
fi
