---
paths:
  - "ansible/**/*.yml"
  - "ansible/**/*.yaml"
  - "ansible/**/*.j2"
---

# Ansible Rules

- All tasks must be **idempotent** — rerunning should be side-effect-free
- Use specific modules (`ansible.builtin.apt`, `ansible.builtin.copy`, etc.) over `shell`/`command`
- Give all tasks meaningful names
- Use `ansible-lint` before committing playbooks
- Jinja2 templating (`{{ var }}`) for all variables
- Global vars go in `ansible/inventory/group_vars/all.yml`; per-host overrides in
  `ansible/inventory/host_vars/`.
- Put `no_log: true` on any task that handles a secret or credential, and never print secret values.
- Dry-run with `--check` before applying changes that touch production state.
