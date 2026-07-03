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

# Route the edit to the render-validator(s) whose output it can change — mirrors the
# prek `validate-compose-templates` / `validate-config-templates` /
# `validate-shell-templates` file scopes so in-session validation matches CI:
#   - compose: a service's compose template, a shared macro (ansible/templates/*.j2),
#     host_vars (containers_list) or group_vars/all
#   - config:  the authelia/traefik/prometheus/grafana bind-mounted config templates
#   - shell:   any Jinja-templated shell script (*.sh.j2)
# group_vars/all.yml feeds all three render contexts, so it runs all three.
# Editing other .j2 files (e.g. homepage services.yaml.j2) triggers nothing.
run_compose=0 run_config=0 run_shell=0
case "$file_path" in
    */templates/docker-compose.yml.j2) run_compose=1 ;;
    */ansible/templates/*.j2) run_compose=1 ;;
    */ansible/inventory/host_vars/*.yml) run_compose=1 ;;
    */ansible/inventory/group_vars/all.yml) run_compose=1 run_config=1 run_shell=1 ;;
    */ansible/roles/containers/authelia/templates/configuration.yml.j2 | \
    */ansible/roles/containers/traefik/templates/config.yml.j2 | \
    */ansible/roles/containers/traefik/templates/traefik.yml.j2 | \
    */ansible/roles/containers/prometheus/templates/prometheus.yml.j2 | \
    */ansible/roles/containers/grafana/templates/loki-config.yml.j2 | \
    */ansible/roles/containers/grafana/templates/promtail-config.yml.j2) run_config=1 ;;
    *.sh.j2) run_shell=1 ;;
    *) exit 0 ;;
esac

ran=""
for pair in "$run_compose:validate_compose_templates" \
            "$run_config:validate_config_templates" \
            "$run_shell:validate_shell_templates"; do
    flag="${pair%%:*}" script="${pair#*:}"
    [[ "$flag" == "1" ]] || continue
    if ! output=$("$UV" run --no-sync --quiet python "scripts/${script}.py" 2>&1); then
        echo "validate-compose: ${script} FAILED after editing $(basename "$file_path"):" >&2
        echo "$output" | grep -E '\[FAIL\]|failure|FAILED' >&2
        exit 2
    fi
    ran="$ran ${script#validate_}"
done

echo "validate-compose: render validation passed (${ran# }) ✓"
exit 0
