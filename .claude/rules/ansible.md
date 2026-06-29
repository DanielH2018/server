---
paths:
  - "ansible/**/*.yml"
  - "ansible/**/*.yaml"
  - "ansible/**/*.j2"
---

# Ansible Rules

The core conventions (idempotency, prefer specific built-in modules over `shell`/`command`,
meaningful task names, `ansible-lint` before committing, where new vars go) live in CLAUDE.md. This
file only adds the path-specific detail not spelled out there:

- Put `no_log: true` on any task that handles a secret or credential, and never print secret values.
- Dry-run with `--check` before applying changes that touch production state.
