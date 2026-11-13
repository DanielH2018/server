# GitHub Copilot Instructions for the Homelab Repository

This repository manages a k3s homelab as code: k3s roles that Flux applies, and the host
packages that configure each host.

> **Source of truth:** this file is a summary. The repo-root `CLAUDE.md` and the per-role
> `CLAUDE.md` files are authoritative — in particular, always run Python tools via `uv run`
> (the repo's pinned env), edit secrets only with `sops`, and run
> `prek run --all-files` for lint/validation. If anything here disagrees with `CLAUDE.md`,
> follow `CLAUDE.md`.

## Repository Structure
- **Host bring-up**: `hosts/bootstrap-host.sh` brings a host up, and the host packages under `hosts/` carry its setup plane. `ansible/` holds no playbook for it.
- **Roles**: Located in `k8s/roles/`. New services are k3s workloads under `k8s/roles/`; the Pi's Docker services live in `hosts/components/pi-compose/services/`.
- **Inventory**: Host-specific variables are in `inventory/host_vars`. Shared variables are in `inventory/group_vars`.
- **Secrets**: Sensitive data is in `secrets/secrets.yml` (SOPS-encrypted).
- **Rendered output**: the Pi's Compose project is rendered into its host package from `hosts/components/pi-compose/` — never edit it on the host.

## Development Guidelines

### Version Control
- **Ignored Files**: Ensure runtime data, caches, logs, and other non-configuration files are excluded from the repository via `.gitignore`.
- **State vs Configuration**: Only commit configuration files and templates. Runtime state should be managed on the target server, not in the repository.

### Container Management
- When adding a new service, default to k3s: follow the `new-k8s-service` skill
  (`.claude/skills/new-k8s-service/SKILL.md`).
- A Docker service on `daniel-pi` follows the `new-container` skill
  (`.claude/skills/new-container/SKILL.md`): a `compose_services` entry in
  `inventory/host_vars/daniel-pi.yml` and a
  `hosts/components/pi-compose/services/<name>/compose.yaml.j2`.
- Put any secrets in `secrets/secrets.yml` (edit via `sops`) and reference them
  as `{{ variable_name }}` in templates.

### Variables and Secrets
- Use Jinja2 templating (`{{ var }}`) for variables.
- **Encryption**: Use `sops` with `age` for encrypting sensitive files. Per the `.sops.yaml` configuration, any `.yml` or `.yaml` files inside a `vars/` or `secrets/` directory will be encrypted.
- **Editing Secrets**: To edit an encrypted file, use the command `sops path/to/encrypted/file.yml`.
- **Decryption**: Flux decrypts the cluster's own SOPS file with the cluster's age key, and each host decrypts its own per-host file under `secrets/hosts/` with that host's key (ADR 0022, ADR 0029).
