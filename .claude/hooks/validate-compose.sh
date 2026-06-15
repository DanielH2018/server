#!/bin/bash
# PostToolUse hook: validate compose-template rendering after editing a
# docker-compose.yml.j2 or a shared macro it includes.
#
# ansible-lint does NOT catch the failure mode this guards: Jinja whitespace /
# indentation corruption that renders to malformed YAML and silently fails to
# recreate the container. scripts/validate_compose_templates.py renders every
# service's compose template (mirroring Ansible's trim/lstrip_blocks) and parses
# the YAML — it is the only thing that catches those bugs before CI. This wires
# it into the edit loop so the failure surfaces in-session, not on push.
#
# Quiet on success; on failure, prints the [FAIL] lines to stderr and exits 2 so
# Claude sees that the edit broke template rendering.

input=$(cat)

cd /home/ubuntu/server || exit 0
UV=/home/ubuntu/.local/bin/uv

# Route through uv so the project-pinned interpreter parses the hook JSON (not the
# system python3); --no-sync keeps it fast.
file_path=$(echo "$input" | "$UV" run --no-sync --quiet python -c "
import sys, json
data = json.load(sys.stdin)
print((data.get('tool_input', {}) or {}).get('file_path', ''))
" 2>/dev/null || echo "")

[[ -z "$file_path" ]] && exit 0

# Only run for compose templates or the shared macros they include — editing
# other .j2 files (e.g. homepage services.yaml.j2) does not affect compose YAML.
case "$file_path" in
    */templates/docker-compose.yml.j2) ;;
    */ansible/templates/*.j2) ;;
    *) exit 0 ;;
esac

if ! output=$("$UV" run --no-sync --quiet python scripts/validate_compose_templates.py 2>&1); then
    echo "validate-compose: template rendering FAILED after editing $(basename "$file_path"):" >&2
    echo "$output" | grep -E '\[FAIL\]|failure' >&2
    exit 2
fi

echo "validate-compose: all compose templates render to valid YAML ✓"
exit 0
