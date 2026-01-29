# GitHub Copilot Instructions for Ansible Repository

This repository contains Ansible playbooks and roles for server management.

## Repository Structure
- **Playbooks**: Located in the root (e.g., `initial_setup.yml`, `deploy.yml`).
- **Roles**: Located in `roles/`. New container services should be added as roles in `roles/containers/`.
- **Inventory**: Host-specific variables are in `inventory/host_vars`. Shared variables are in `inventory/group_vars`.
- **Secrets**: Sensitive data is in `secrets.yml`.
- **Configuration**: Configuration files are in `config/` and in the project root.

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
- When adding a new container:
    1. Create a role in `roles/containers/<name>`.
    2. Define tasks in `roles/containers/<name>/tasks/main.yml`.
    3. Use `docker-compose.yml.j2` templates in `roles/containers/<name>/templates/` if using Docker Compose.
    4. Add necessary environment variables to `.env` (managed via templates).
    5. Add the role to `deploy.yml` with appropriate tags.

### Variables and Secrets
- Use Jinja2 templating (`{{ var }}`) for variables.
- **Encryption**: Use `sops` with `age` for encrypting sensitive files. Per the `.sops.yaml` configuration, any `.yml` or `.yaml` files inside a `vars/` or `secrets/` directory will be encrypted.
- **Editing Secrets**: To edit an encrypted file, use the command `sops path/to/encrypted/file.yml`.
- **Automation**: The `community.sops.sops_decrypt` lookup is used in playbooks to decrypt data at runtime.

### Dependency Management
- **Collections and Roles**: Use `requirements.yml` to manage external Ansible collections and roles.

### Testing and CI/CD
- **Dry Run**: Use `--check` mode to verify playbooks before applying changes where possible.
