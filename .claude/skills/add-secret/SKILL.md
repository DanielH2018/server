---
name: add-secret
description: Add a new SOPS-encrypted secret to ansible/vars/secrets.yml and register it for rotation tracking. Use when the user wants to add a credential, token, or password that a template will reference.
allowed-tools: Bash, Read, Edit
disable-model-invocation: true
---

Add a new secret end-to-end, following the repo's documented flow (CLAUDE.md "Secrets
Management" + `docs/secret-rotation.md`). Run everything from `/home/ubuntu/server`.

**Never print or echo a secret value.** It must not land in the transcript, the shell
history, or a log. When a value must be supplied by the user, hand them the command to run
themselves (via the `! ` prefix) rather than putting the value in a command you run.

## Inputs
- `name` — the secret variable name (snake_case; how templates will reference it as `{{ name }}`).
- The value, OR a request to generate one (e.g. a 32-byte token).

## Steps

1. **Check it doesn't already exist.** `Bash(sops -d ansible/vars/secrets.yml)` is allow-listed
   for inspection — confirm the key isn't already present. If it is, stop and tell the user.

2. **Classify the tier** (this changes how you add it):
   - **pinned** (`kopia_password`, `authelia_storage_encryption_key`, and similar break-glass
     keys): **DO NOT** `sops set` these — rotating/altering them is a DANGER procedure with
     data-loss risk. Stop and point the user at the `pinned` runbook in `docs/secret-rotation.md`.
   - **auto** — locally generated, no external coupling (e.g. a push token). Safe to generate.
   - **assisted / external** — value comes from a provider console or has an app-side step.

3. **Add the secret** (`.sops.yaml` auto-encrypts anything under `vars/`, so the file stays
   encrypted on disk):
   - **Preferred (value stays private):** tell the user to run it themselves —
     `! sops ansible/vars/secrets.yml` — add the `name: value` line in the editor, save, exit.
   - **Generated value (auto tier):** generate and set without echoing the value:
     `openssl rand -base64 32 | { read v; sops set ansible/vars/secrets.yml "[\"<name>\"]" "\"$v\""; }`
     (the value is never printed). `Bash(openssl rand *)` and `Bash(sops set *)` are allow-listed.
   - **User-provided value via sops set:** only if the user explicitly accepts that the value
     will appear in the command — warn them first.

4. **Verify encryption** — re-run `sops -d ansible/vars/secrets.yml` and confirm the new key
   is present and decrypts. Confirm the on-disk file is still ciphertext (the value is NOT in
   `git diff` as plaintext).

5. **Reference it in the template** (if a service uses it): edit the role's
   `ansible/roles/containers/<svc>/templates/*.j2` to use `{{ name }}`. Add `no_log: true` to
   any task that handles it. (Per `.claude/rules/secrets.md`.)

6. **Register for rotation tracking:** `uv run python scripts/secret_rotation.py sync` — this
   reconciles `ansible/secret_rotation.yml` (plaintext registry: names/tiers/dates, no values)
   with the live secret names and assigns a tier + staggered due-date. Then
   `uv run python scripts/secret_rotation.py audit` to confirm it registered cleanly.

7. **Commit** — stage the plaintext registry change, the (still-encrypted) secrets file, and
   any template edit, then commit. Suggested message body ends with the repo's Co-Authored-By
   trailer. Do **not** deploy unless the user asks (the `/deploy` skill handles that).

## Done when
- The key decrypts from `secrets.yml`, the on-disk file is ciphertext, `secret_rotation.py audit`
  shows it registered, and the change is committed. Report which template now references it and
  which deploy tag would apply it.
