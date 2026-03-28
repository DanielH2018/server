---
paths:
  - "ansible/vars/**"
  - "ansible/inventory/**"
---

# Secrets Handling Rules

- NEVER suggest editing `ansible/vars/secrets.yml` directly — it is SOPS-encrypted
- Always edit secrets with: `sops ansible/vars/secrets.yml`
- New secrets go in `ansible/vars/secrets.yml` and are referenced in templates as `{{ secret_name }}`
- The `community.sops.sops_decrypt` lookup decrypts values at playbook runtime
- Per `.sops.yaml`, any `.yml`/`.yaml` inside a `vars/` or `secrets/` directory is automatically encrypted on save
- Never log or print secret values — use `no_log: true` on sensitive tasks
