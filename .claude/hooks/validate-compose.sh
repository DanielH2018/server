#!/bin/bash
# PostToolUse hook: validate compose-template rendering after editing a
# docker-compose.yml.j2 or a shared macro it includes.
#
# ansible-lint does NOT catch the failure mode this guards: Jinja whitespace /
# indentation corruption that renders to malformed YAML and silently fails to
# recreate the container. scripts/validate/validate_compose_templates.py renders every
# service's compose template (mirroring Ansible's trim/lstrip_blocks) and parses
# the YAML — it is the only thing that catches those bugs before CI. This wires
# it into the edit loop so the failure surfaces in-session, not on push.
#
# Quiet on success; on failure, prints the [FAIL] lines to stderr and exits 2 so
# Claude sees that the edit broke template rendering.

input=$(cat)

# The uv env lives in the primary checkout only; worktrees have no synced venv, so
# `uv run --no-sync` from one fails on the yaml import. Stay here for the interpreter
# and reach into the owning checkout by script path instead (see $repo_root below).
PRIMARY=/home/ubuntu/server
cd "$PRIMARY" || exit 0
UV=/home/ubuntu/.local/bin/uv

# Reading one JSON field used to cost a `uv run --no-sync python` startup -- 37ms of this
# hook's 37ms on an edit the case block below then declines, on EVERY Edit and Write
# (~2,200 in the week of 2026-08-07). jq does it in 2ms. uv is still needed further down,
# where it runs the validators themselves and the pinned interpreter genuinely matters.
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")

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
    # The per-file config arm listed authelia, traefik, prometheus and grafana templates,
    # every one of which retired with its Docker role. validate_config_templates.py's own
    # CONFIG_TEMPLATES list is empty for the same reason and says so ("may be empty between
    # config-bearing eras"). group_vars/all.yml above still triggers the config render, so
    # re-adding a path here is all a future config-bearing template needs.
    *.sh.j2) run_shell=1 ;;
    *) exit 0 ;;
esac

# Validate the checkout that owns the edited file, not always the primary one. The
# validators resolve the repo they render from their own __file__ (scripts/lib/_render_guard.py),
# so passing an absolute path into a .claude/worktrees/<name>/ checkout is what redirects
# them — the working directory does not. Running them relatively rendered the primary
# checkout's templates and reported a pass for a file it never read.
repo_root=$(git -C "$(dirname "$file_path")" rev-parse --show-toplevel 2>/dev/null)
[[ -z "$repo_root" ]] && repo_root="$PRIMARY"

ran=""
for pair in "$run_compose:validate_compose_templates" \
            "$run_config:validate_config_templates" \
            "$run_shell:validate_shell_templates"; do
    flag="${pair%%:*}" script="${pair#*:}"
    [[ "$flag" == "1" ]] || continue
    if ! output=$("$UV" run --no-sync --quiet python "$repo_root/scripts/${script}.py" 2>&1); then
        echo "validate-compose: ${script} FAILED after editing $(basename "$file_path"):" >&2
        echo "$output" | grep -E '\[FAIL\]|failure|FAILED' >&2
        exit 2
    fi
    ran="$ran ${script#validate_}"
done

echo "validate-compose: render validation passed (${ran# }) ✓"
exit 0
