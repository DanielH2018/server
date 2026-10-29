# GitHub Copilot Instructions for Ansible Repository

This repository contains Ansible playbooks and roles for server management.

> **Source of truth:** this file is a summary. The repo-root `CLAUDE.md` and the per-role
> `CLAUDE.md` files are authoritative — in particular, always run Ansible via `uv run
> ansible-playbook` (the repo's pinned env), edit secrets only with `sops`, and run
> `prek run --all-files` for lint/validation. If anything here disagrees with `CLAUDE.md`,
> follow `CLAUDE.md`.

## Repository Structure
- **Playbooks**: Located in `ansible/` (e.g., `ansible/initial_setup.yml`, `ansible/deploy.yml`).
- **Roles**: Located in `ansible/roles/`. New services are k3s workloads under `ansible/roles/k8s/`; the Pi's Docker services live in `hosts/components/pi-compose/services/`.
- **Inventory**: Host-specific variables are in `ansible/inventory/host_vars`. Shared variables are in `ansible/inventory/group_vars`.
- **Secrets**: Sensitive data is in `ansible/vars/secrets.yml` (SOPS-encrypted).
- **Rendered output**: the Pi's Compose project is rendered into its host package from `hosts/components/pi-compose/` — never edit it on the host.

## Development Guidelines

### Ansible Best Practices
- **Idempotency**: Ensure tasks are idempotent. Running a playbook multiple times should not have side effects.
- **Modules**: Use specific Ansible modules (e.g., `ansible.builtin.apt`, `ansible.builtin.copy`, `ansible.builtin.template`) instead of `shell` or `command` where possible.
- **Naming**: Give meaningful names to all tasks.
- **Linting**: Use `ansible-lint` to verify playbooks and roles against best practices.

### Version Control
- **Ignored Files**: Ensure runtime data, caches, logs, and other non-configuration files are excluded from the repository via `.gitignore`.
- **State vs Configuration**: Only commit configuration files and templates. Runtime state should be managed on the target server, not in the repository.

### Container Management
- When adding a new service, default to k3s: follow the `new-k8s-service` skill
  (`.claude/skills/new-k8s-service/SKILL.md`).
- A Docker service on `daniel-pi` follows the `new-container` skill
  (`.claude/skills/new-container/SKILL.md`): a `compose_services` entry in
  `ansible/inventory/host_vars/daniel-pi.yml` and a
  `hosts/components/pi-compose/services/<name>/compose.yaml.j2`.
- Put any secrets in `ansible/vars/secrets.yml` (edit via `sops`) and reference them
  as `{{ variable_name }}` in templates.

### Variables and Secrets
- Use Jinja2 templating (`{{ var }}`) for variables.
- **Encryption**: Use `sops` with `age` for encrypting sensitive files. Per the `.sops.yaml` configuration, any `.yml` or `.yaml` files inside a `vars/` or `secrets/` directory will be encrypted.
- **Editing Secrets**: To edit an encrypted file, use the command `sops path/to/encrypted/file.yml`.
- **Automation**: The `community.sops.sops_decrypt` lookup is used in playbooks to decrypt data at runtime.

### Dependency Management
- **Collections and Roles**: Use `requirements.yml` to manage external Ansible collections and roles.

### Testing and CI/CD
- **Dry Run**: Use `--check` mode to verify playbooks before applying changes where possible.
