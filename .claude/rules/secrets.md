---
paths:
  - "secrets/**"
  - "inventory/**"
  - "secrets/.sops.yaml"
---

# Secrets Rules

The root `CLAUDE.md`, *Secrets Management*, is the home of the safety rules: what `git diff` of
`secrets.yml` prints, never decrypt, and never commit plaintext. This file is the home of the
`.sops.yaml` and key-check mechanics, and of the enforcement detail these paths carry. A template
reads a value as `{{ name }}`. Flux decrypts the cluster's SOPS file with the cluster's age key
(ADR 0022), and each host decrypts its own file under `secrets/hosts/` with that host's key
(ADR 0029).

- A direct Edit/Write to `secrets.yml` is **denied by the block-protected-edits hook** (content-based
  detection of the SOPS markers) — there's no way to "just quickly edit" it; use `sops` / `/add-secret`.
- `secrets/.sops.yaml` is tracked and holds public keys only. It lists the age recipients new and
  updated secrets are encrypted to. SOPS searches upward from the file, so the config sits at
  `ansible/`, not the repo root.
- `.sops.yaml` auto-encrypts a `.yml` or `.yaml` on save only inside a `vars/` or `secrets/`
  directory. **`inventory/` is NOT auto-encrypted and NOT hook-guarded** — a secret pasted into
  `host_vars`/`group_vars` would be committed in plaintext (gitleaks may catch it, but don't rely
  on that). Keep secrets in `vars/secrets.yml` and reference them from inventory, never inline them.
- Key names stay plaintext in the encrypted file, so `grep -c '^<name>:'` on it checks that a key
  exists, and `grep -c '^<name>: ENC\['` checks that its value is ciphertext. Both print a count,
  never a value.
