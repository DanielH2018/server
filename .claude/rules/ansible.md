---
paths:
  - "ansible/**/*.yml"
  - "ansible/**/*.yaml"
  - "ansible/**/*.j2"
---

# Ansible Rules

- All tasks must be idempotent — rerunning the playbook must have no side effects
- Use specific built-in modules (`ansible.builtin.apt`, `ansible.builtin.copy`, `ansible.builtin.template`) instead of `shell` or `command` wherever possible
- Give every task a meaningful name
- Use `no_log: true` on any task that handles secrets or credentials
- Dry run with `--check` before applying changes to production
- Run `ansible-lint` before committing any playbook or role changes
- New variables go in `ansible/inventory/group_vars/all.yml` (global) or `ansible/inventory/host_vars/<host>.yml` (host-specific)
- Secrets are never hardcoded — always reference from `vars/secrets.yml` via the `community.sops.sops_decrypt` lookup
