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
2. Add a `docker-compose.yml.j2` template in `ansible/roles/containers/<name>/templates/`
3. Add the role to `ansible/deploy.yml` with a tag matching the service name
4. Add any secrets to `ansible/vars/secrets.yml` (edit with `sops ansible/vars/secrets.yml`)
5. Reference secrets via `{{ variable_name }}` in templates

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
- Timezone: `America/New_York`
- Containers should have healthchecks defined where possible

## Pre-commit Hooks
The repo uses pre-commit with YAML linting, Ansible linting, and gitleaks (secret scanning).
Run `pre-commit run --all-files` to check before committing.

## Variables
Global vars in `ansible/inventory/group_vars/all.yml`. Per-host overrides in `ansible/inventory/host_vars/`.
