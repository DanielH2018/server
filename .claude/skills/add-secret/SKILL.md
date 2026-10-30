---
name: add-secret
description: Add a new SOPS-encrypted secret to ansible/vars/secrets.yml and register it for rotation tracking. Use when the user wants to add a credential, token, or password that a template will reference.
allowed-tools: Bash, Read, Edit
disable-model-invocation: false
---

Add a new secret end-to-end, following the repo's documented flow (CLAUDE.md "Secrets
Management" + `docs/secret-rotation.md`). Run everything from `/home/ubuntu/server`.

**Never print or echo a secret value.** It must not land in the transcript, the shell
history, or a log. When a value must be supplied by the user, hand them the command to run
themselves (via the `! ` prefix) rather than putting the value in a command you run.

## Inputs
- `name` — the secret variable name (snake_case; how templates will reference it as `{{ name }}`).
- The value, OR a request to generate one (e.g. a 32-byte token).

## Which stores the secret touches

What reads the secret decides which stores it touches. Each store has its own writer
(`docs/target-architecture/deploy/d3-secrets.md`, *What one secret costs*):

- **`ansible/vars/secrets.yml`** holds every secret. You write it in step 3.
- **`ansible/vars/cluster-vars.sops.env`** holds each value a cluster render substitutes.
  It holds every key any k8s template references, not only the keys of Flux-owned
  services. Only the operator's `cluster_vars.py write` writes it (**operator-only**,
  because it decrypts).
- **A SOPS manifest under `ansible/vars/cluster-manifests/`** holds the values of a Secret
  that Flux must not substitute. `write` writes it too.
- **A per-host SOPS file** holds the values its host reads. Its writer is the host-package
  plan's, not this skill's.
- **`flux-system/sops-age`** holds the cluster's private age key and no application value.
  Adding a secret never touches it.

So a secret that only Ansible reads (a Pi compose template, a setup role) touches
`secrets.yml` alone. A secret that a k8s template references also touches a cluster file,
which only the operator can write. Step 6 tells you which case you are in.

## Steps

1. **Check it doesn't already exist.** SOPS encrypts values but leaves key names in plaintext,
   so `grep -c '^<name>:' ansible/vars/secrets.yml` answers without decrypting anything. `0`
   means the name is free. If it prints `1`, stop and tell the user. Never decrypt the file
   to inspect it; CLAUDE.md *Secrets Management* has why.

2. **Classify the tier** (this changes how you add it):
   - **pinned** (`authelia_storage_encryption_key` and similar break-glass
     keys): **DO NOT** `sops set` these — rotating/altering them is a DANGER procedure with
     data-loss risk. Stop and point the user at the `pinned` runbook in `docs/secret-rotation.md`.
   - **auto** — locally generated, no external coupling (e.g. a push token). Safe to generate.
   - **assisted / external** — value comes from a provider console or has an app-side step.

3. **Add the secret** (`.sops.yaml` auto-encrypts anything under `vars/`, so the file stays
   encrypted on disk):
   - **Preferred (value stays private):** tell the user to run it themselves —
     `! sops ansible/vars/secrets.yml` — add the `name: value` line in the editor, save, exit.
   - **A Kuma push token is 32 hex characters, not base64.** AutoKuma validates the format and
     refuses the monitor outright — `Invalid push_token, push token should be 32 characters and
     contain only letters and numbers` in the autokuma sidecar's log — so the tile is never
     created, every push to it is lost, and the only visible symptom is `kuma-drift` reporting
     it "declared, not live" plus a failing `kuma-status-page-sync` job. A base64 value has
     `+`, `/` and `=` in it and is 44 characters. `secret_rotation.py rotate` already mints
     `token_hex(16)` for exactly this reason. The uptime-kuma deploy asserts the format and
     names the refused tile, but only once someone deploys it. Match it on the first write:
     `openssl rand -hex 16 | { read v; sops set ansible/vars/secrets.yml "[\"<name>\"]" "\"$v\""; }`
   - **Any other generated value (auto tier):** generate and set without echoing the value:
     `openssl rand -base64 32 | { read v; sops set ansible/vars/secrets.yml "[\"<name>\"]" "\"$v\""; }`
     (the value is never printed). Neither settings file allow-lists these commands, so expect
     a permission prompt.
   - **User-provided value via sops set:** only if the user explicitly accepts that the value
     will appear in the command — warn them first.

4. **Verify encryption** — `grep -c '^<name>: ENC\[' ansible/vars/secrets.yml` must print `1`.
   That proves the key exists and its stored value is SOPS ciphertext, and it prints a count,
   never a value. Do not run `git diff` on the file: its `diff=sops` driver decrypts before
   diffing. The first deploy that reads the value proves it decrypts.

5. **Reference it in the template** (if a service uses it): edit the role's
   `ansible/roles/k8s/<svc>/templates/*.j2` to use `{{ name }}`. A Pi Docker service reads a
   secret through its package instead: add the key to `keys:` on the `compose.env` output in
   `hosts/components/pi-compose/component.yml`, and the operator runs
   `host_secrets.py sync daniel-pi` (**operator-only**). Add `no_log: true` to
   any task that handles it. (Per `.claude/rules/secrets.md`.)

6. **Check whether the cluster reads it.** Run
   `uv run python scripts/secrets_mgmt/cluster_vars.py check`. It reads no plaintext and
   needs no key. If it fails naming `<name>`, for example `the cluster render references
   <name>, and the cluster file does not hold it`, the key is cluster-referenced. Then:
   - Finish steps 7 and 8, but **stop before opening the PR**. CI runs the same `check`
     and fails until the cluster file holds the key.
   - Hand the operator the next step: `uv run python scripts/secrets_mgmt/cluster_vars.py
     write` on this branch (**operator-only**), then a commit of the cluster files it
     rewrote. Never run `write` yourself, and never hand-edit a cluster file: a hand edit
     breaks its SOPS MAC.
   - Name the branch and the key in your report, so the operator can pick it up.

   If `check` prints `clean`, no cluster render references the key, and you may open the PR.
   If it fails naming only other keys, the tree was already stale: report that instead.

7. **Register for rotation tracking:** `uv run python scripts/secrets_mgmt/secret_rotation.py sync` — this
   reconciles `ansible/secret_rotation.yml` (plaintext registry: names/tiers/dates, no values)
   with the live secret names and assigns a tier + staggered due-date. `sync` knows only
   `*_push_token` (`auto`) and `*_user`/`*_username` (`ignore`); every other name lands in
   `assisted`. **If step 2 classified the key `external` or `pinned`, edit its `tier` in the
   registry now** — `sync` preserves the edit. Then
   `uv run python scripts/secrets_mgmt/secret_rotation.py audit` to confirm it registered cleanly.

8. **Commit** — stage the plaintext registry change, the (still-encrypted) secrets file, and
   any template edit, then commit. Suggested message body ends with the repo's Co-Authored-By
   trailer. Do **not** deploy unless the user asks (the `/deploy` skill handles that).

## Done when
- Step 4's `grep -c '^<name>: ENC\['` prints `1`, `secret_rotation.py audit`
  shows it registered, and the change is committed. Report which template now references it and
  which deploy tag would apply it.
- For a cluster-referenced key, step 6's handoff is in the report, and no PR is open yet.

## Onboarding a host that cannot decrypt yet

A host with no age key of its own fails `initial_setup.yml` and `deploy.yml` at their
secret-load `pre_task`, which reads as a playbook bug rather than a missing key. No playbook
creates that key, so the first step is by hand; `ansible/README.md` §5 has the full flow.

1. On the new host, as the operator, run `age-keygen -o ~/.config/sops/age/keys.txt` after
   creating the directory 0700. It prints the **public** key.
2. Add that public key to `ansible/.sops.yaml` — tracked, and public keys only.
3. On a host that can already decrypt: `sops updatekeys ansible/vars/secrets.yml`.
4. Commit and push, then `git pull` on the new host.

Multi-recipient is **OR**: any listed key decrypts the whole file, so adding one grants the
new host everything. The private key never leaves `~/.config/sops/age/keys.txt` on the host
that generated it. `docs/break-glass.md`'s *The age keys* says which hosts hold one.
