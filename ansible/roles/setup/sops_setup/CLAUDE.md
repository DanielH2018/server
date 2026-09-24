# sops_setup — install SOPS/age + generate the host's decrypt key

Installs the `sops`/`age` toolchain, the pinned Ansible collections, and generates the
host's own age keypair so it can decrypt `ansible/vars/secrets.yml`. **Not a container
role** — a host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not
`deploy.yml`. See repo-root `CLAUDE.md` (§ Secrets Management) for the bigger picture.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `bootstrap.yml --tags "sops_setup"`; `initial_setup.yml --tags "sops_setup"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where it runs
- In `ansible/initial_setup.yml`, after [[config_files]] / [[initial_setup]] — every host.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "sops_setup"`.
- **Granular tags:** `sops-install` (age + sops binary), `collections` (pinned galaxy
  install), `age-key` (key-dir/keygen/pubkey display + the first-host `.sops.yaml` seed —
  the seed shares the tag because it consumes the registered pubkey).

## What it does (`tasks/main.yml`)
1. **Install** `age` (apt) and the `sops` binary to `/usr/local/bin`, at
   `defaults/main.yml:sops_setup_version` and arch-mapped amd64/arm64. The `get_url` is
   **sha256-checksum-pinned** (SOPS is the root of the secret-decryption trust chain — an
   unverified binary would be a supply-chain hole). Bump the version and both per-arch
   sha256s **together**: `uv run python scripts/validate/asset_pins.py --only
   sops_setup_binary_amd64,sops_setup_binary_arm64` fetches each URL and prints the hash that
   does not match, and the release's `sops-<ver>.checksums.txt` is the same answer upstream.
   The pin lives in `defaults/main.yml` so `asset_pins.py` can see it at all — that checker
   reads defaults alone, and the URL was inline in `tasks/` until #2313. One url+digest pair
   per architecture, because a digest conditional on `ansible_facts.architecture` renders as
   an unresolved template under the checker's `StrictUndefined`.
2. **Install pinned collections** from `requirements.yml` into `ansible/collections` (the
   path `ansible.cfg` loads from, matching the prek lint hook) — run as the repo owner
   (`become: false`) so `community.sops` etc. land for the user who runs deploys.
3. **Generate the age key** at `~/.config/sops/age/keys.txt` (`creates:`-guarded → idempotent,
   won't regenerate) and print its public key.
4. **Seed `ansible/.sops.yaml`** with that pubkey — **first-host bootstrap only**
   (skipped when the tracked `.sops.yaml` already exists; see Notable).
This role does **not** write `SOPS_AGE_KEY_FILE` into `~/.bashrc`. Until #2319 it appended
the export with `lineinfile`, which landed after the dotfiles' `ble-attach` line and was undone
by every `chezmoi apply`. The export now belongs to whatever owns the host's `.bashrc`: the
dotfiles repo's `home/dot_bashrc` on the chezmoi hosts, and [[config_files]]' tracked
`files/.bashrc` on daniel-pi.

## Notable
- **DR:** host keys at `~/.config/sops/age/keys.txt` are in **no automated backup** (nothing
  outside a Longhorn volume is backed up at all since Kopia retired 2026-08-13, and Kopia only
  covered `containers/` before that); they're backed up out-of-band (2026-06-06). Since
  **2026-06-11** `.sops.yaml` also lists an **off-box recovery recipient** (private half
  only in the operator's password manager), so the secrets survive losing both hosts.
  Gotcha: `sops updatekeys` resolves `.sops.yaml` from the CWD — run it from `ansible/`.
- **Onboarding an Nth host is NOT this role** — `.sops.yaml` is tracked, so every checkout
  already has it and step 4 self-skips. Adding a host = run `ansible/bootstrap.yml` on it,
  add its pubkey to `.sops.yaml`, `sops updatekeys`, commit/pull. See `bootstrap.yml` header
  and [[gitops_deploy]] only consume secrets after this is in place.
- **Chicken-and-egg:** `initial_setup.yml`'s `Load encrypted secrets` pre_task needs SOPS to
  already work, so a brand-new host must run `bootstrap.yml` first (it has no secret
  dependency) before this role/playbook can succeed.
