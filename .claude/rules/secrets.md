---
paths:
  - "ansible/vars/**"
  - "ansible/inventory/**"
---

# Secrets Rules

CLAUDE.md covers the SOPS/age secrets workflow (edit `ansible/vars/secrets.yml` via `sops` or the
`/add-secret` skill; reference values as `{{ name }}` through the `sops_decrypt` lookup). This file
only adds the path-specific enforcement detail not spelled out there:

- A direct Edit/Write to `secrets.yml` is **denied by the block-protected-edits hook** (content-based
  detection of the SOPS markers) — there's no way to "just quickly edit" it; use `sops` / `/add-secret`.
- `.sops.yaml` auto-encrypts on save only inside a `vars/` or `secrets/` directory. **`inventory/` is
  NOT auto-encrypted and NOT hook-guarded** — a secret pasted into `host_vars`/`group_vars` would be
  committed in plaintext (gitleaks may catch it, but don't rely on that). Keep secrets in
  `vars/secrets.yml` and reference them from inventory, never inline them.
