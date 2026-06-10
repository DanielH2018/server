# sops_setup — install SOPS/age + generate the host's decrypt key

Installs the `sops`/`age` toolchain, the pinned Ansible collections, and generates the
host's own age keypair so it can decrypt `ansible/vars/secrets.yml`. **Not a container
role** — a host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not
`deploy.yml`. See repo-root `CLAUDE.md` (§ Secrets Management) for the bigger picture.

## Where it runs
- In `ansible/initial_setup.yml`, after [[config_files]] / [[initial_setup]] — every host.
- `ansible-playbook ansible/initial_setup.yml --tags "sops_setup"`.

## What it does (`tasks/main.yml`)
1. **Install** `age` (apt) and the `sops` binary (pinned `v3.9.2`, arch-mapped amd64/arm64)
   to `/usr/local/bin`.
2. **Install pinned collections** from `requirements.yml` into `ansible/collections` (the
   path `ansible.cfg` loads from, matching the prek lint hook) — run as the repo owner
   (`become: false`) so `community.sops` etc. land for the user who runs deploys.
3. **Generate the age key** at `~/.config/sops/age/keys.txt` (`creates:`-guarded → idempotent,
   won't regenerate) and print its public key.
4. **Seed `ansible/.sops.yaml`** with that pubkey — **first-host bootstrap only**
   (skipped when the tracked `.sops.yaml` already exists; see Notable).
5. **Export `SOPS_AGE_KEY_FILE`** in `~/.bashrc` so `sops`/the lookup find the key.

## Notable
- **DR single point of failure:** the private key at `~/.config/sops/age/keys.txt` is the
  ONLY thing that can decrypt the secrets, and it is in **no automated backup** (Kopia backs
  up only `containers/`). It must be kept off-box (password manager / hardware token).
  Confirmed backed up out-of-band 2026-06-06 (see the in-file `DR NOTE`). A second `age`
  recipient in `.sops.yaml` would remove the single-key dependency — not yet done.
- **Onboarding an Nth host is NOT this role** — `.sops.yaml` is tracked, so every checkout
  already has it and step 4 self-skips. Adding a host = run `ansible/bootstrap.yml` on it,
  add its pubkey to `.sops.yaml`, `sops updatekeys`, commit/pull. See `bootstrap.yml` header
  and [[gitops_deploy]] only consume secrets after this is in place.
- **Chicken-and-egg:** `initial_setup.yml`'s `Load encrypted secrets` pre_task needs SOPS to
  already work, so a brand-new host must run `bootstrap.yml` first (it has no secret
  dependency) before this role/playbook can succeed.
