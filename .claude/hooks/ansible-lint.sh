#!/bin/bash
# PostToolUse hook: run ansible-lint after editing Ansible YAML files

# Read hook input from stdin
input=$(cat)

# Extract the file path from tool input
file_path=$(echo "$input" | python3 -c "
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
    cd /home/ubuntu/server
    /home/ubuntu/.local/bin/ansible-lint "$file_path" 2>&1
fi
