#!/bin/bash
# PostToolUse hook: run ansible-lint after editing Ansible YAML files

# Read hook input from stdin
input=$(cat)

cd /home/ubuntu/server || exit 0
UV=/home/ubuntu/.local/bin/uv

# Extract the file path from tool input. Routed through uv so the project-pinned
# interpreter parses the hook JSON (not the system python3); --no-sync keeps it fast.
file_path=$(echo "$input" | "$UV" run --no-sync --quiet python -c "
import sys, json
data = json.load(sys.stdin)
tool_input = data.get('tool_input', {})
print(tool_input.get('file_path', ''))
" 2>/dev/null || echo "")

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
