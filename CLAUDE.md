# Server Homelab — Claude Code Context

## Project Overview
Docker-based homelab managed with Ansible. ~42 containerized services deployed across two hosts via infrastructure-as-code.

**Hosts:**
- `daniel-server` — main server (Intel XE graphics, LVM storage)
- `daniel-pi` — Raspberry Pi

**Key technologies:** Docker Compose, Ansible, Traefik (reverse proxy), Cloudflare DNS, Authelia (SSO), SOPS/age (secret encryption)

## Directory Structure
```
ansible/          # Ansible playbooks, roles, inventory, templates  ← EDIT HERE
containers/       # Docker Compose definitions deployed by Ansible  ← DO NOT EDIT
scripts/          # Python helper scripts
files/            # Data migration utilities
```

> **`containers/` is read-only.** Files here are generated and deployed by Ansible from templates in `ansible/roles/containers/*/templates/`. Any direct edits will be overwritten on the next deploy. Always modify the corresponding Ansible role template instead.

## Adding a New Container Service
1. Create `ansible/roles/containers/<name>/tasks/main.yml`
2. Add a `docker-compose.yml.j2` template in `ansible/roles/containers/<name>/templates/`.
   Use the shared macros in `ansible/templates/` rather than hand-rolling boilerplate:
   `traefik.yml.j2` (`labels`), `autokuma.yml.j2` (`kuma`), `healthcheck.yml.j2`
   (`healthcheck`), `networks.yml.j2` (`service_networks()` / `external_networks()` —
   the per-service and top-level `networks:` blocks), and `resources.yml.j2`
   (`resources(cpu_limit, mem_limit, cpu_res, mem_res)` — the `deploy.resources` caps).
   The `/new-container` skill has the canonical skeleton.
3. Add the role to `ansible/deploy.yml` with a tag matching the service name
4. Add any secrets to `ansible/vars/secrets.yml` (edit with `sops ansible/vars/secrets.yml`)
5. Reference secrets via `{{ variable_name }}` in templates
6. **If the service bind-mounts an Ansible-templated config file:** `register:` each config
   task with a `<role>_`-prefixed name and pass `common_config_changed: "{{ <reg> is changed }}"`
   (OR several) on the `common`/`docker_deploy` include. Deploys are idempotent (`recreate: auto`
   by default), so without this an edit to that config won't recreate the container. See
   `ansible/roles/containers/common/CLAUDE.md`.

## Common Commands
```bash
# Deploy a specific container
ansible-playbook ansible/deploy.yml --tags "<service-name>"

# Deploy all containers
ansible-playbook ansible/deploy.yml

# Dry run
ansible-playbook ansible/deploy.yml --tags "<service-name>" --check

# Edit encrypted secrets
sops ansible/vars/secrets.yml

# Initial server setup
ansible-playbook ansible/initial_setup.yml
```

## Shell Commands — Shape Them to Auto-Approve
A PreToolUse hook (`.claude/hooks/auto-approve-readonly.py`) auto-approves Bash it can
**prove is read-only**, so those run without a permission prompt. Write exploratory/
read-only commands to fit it. Anything that writes or executes still prompts — that's intended.

**Auto-approves (no prompt):**
- Single read-only commands and pipelines: `grep … | sort | head`
- Read-only stages sequenced with `;`, `&&`, `||`, or newlines: `cd dir && grep … *.j2`
- Write-free redirects: `… 2>/dev/null`, `>/dev/null 2>&1`
- Read-only `git`/`docker`/`find` (no `-exec`/`-delete`) and read-only `awk`/`sed`

**Forces a prompt — restructure, or just accept the one-off prompt:**
- **Command substitution** `$(…)`, backticks, `${…}` — rejected outright. Replace
  `svc=$(echo "$d" | cut -d/ -f4)` with a substitution-free pipeline, or split the step out.
- **Shell control flow** — `for`/`while` loops, `if/then/else/fi`. Prefer one `grep`/`find`/`awk`
  over a loop: e.g. `grep -L "limits:" …/*.j2` (files missing a pattern) + `grep -l "limits:" …`
  (files with it) instead of looping `if grep -q …; then …; fi`.
- **Writes/exec** — `> file`, `tee`, `sed -i`, `sed s///e|w`, `awk 'system()'`/`print > "f"`,
  subshells `(…)`, backgrounding `&`. (Note: `awk` programs containing `>` — even as a
  numeric comparison — are conservatively rejected; use a different test or accept the prompt.)

Source of truth + tests: `.claude/hooks/auto-approve-readonly.py`, `.claude/hooks/test_auto_approve_readonly.py`.

## Secrets Management
- Secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS + age
- `.sops.yaml` auto-encrypts any `.yml`/`.yaml` in `vars/` or `secrets/` directories
- At runtime, `community.sops.sops_decrypt` lookup decrypts values
- **Never commit plaintext secrets**

## Ansible Conventions
- All tasks must be **idempotent** — rerunning should be side-effect-free
- Use specific modules (`ansible.builtin.apt`, `ansible.builtin.copy`, etc.) over `shell`/`command`
- Give all tasks meaningful names
- Use `ansible-lint` before committing playbooks
- Jinja2 templating (`{{ var }}`) for all variables

## Docker Compose Conventions
- All containers use Traefik labels for reverse proxy routing
- Docker network: `proxy`
- PUID/PGID: `1000`/`1000`, user: `ubuntu`
- Timezone: `America/Chicago`
- Containers should have healthchecks defined where possible

## Pre-commit Hooks
The repo uses [prek](https://prek.j178.dev) (config: `prek.toml`) with YAML linting, Ansible linting, and gitleaks (secret scanning).
Run `prek run --all-files` to check before committing.

## Variables
Global vars in `ansible/inventory/group_vars/all.yml`. Per-host overrides in `ansible/inventory/host_vars/`.
