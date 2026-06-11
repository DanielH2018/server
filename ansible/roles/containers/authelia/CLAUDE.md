# authelia — SSO / forward-auth middleware

Provides the authentication layer used by every service with `use_authelia: true`.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `authelia/authelia` (version-pinned, Renovate-managed)
- **Host:** daniel-server
- **Networks:** proxy · **Authelia:** N/A (it *is* Authelia)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Traefik points its `forward-auth` middleware here; toggling a service's
  `use_authelia` flag in `containers_list` is what puts it behind 2FA.
- `templates/configuration.yml.j2` — access control rules, OIDC clients, session/redis.
- **configuration.yml is re-templated on EVERY deploy** (since 2026-06-11; see
  `docs/superpowers/specs/2026-06-11-authelia-retemplatable-config-design.md`). Template
  edits reach the live install and recreate the container via `common_config_changed` —
  no live-file migration tasks needed. The formerly first-run-generated inputs (OIDC
  HMAC secret, client digests, RSA key) now live in `ansible/vars/secrets.yml`:
  `authelia_oidc_hmac_secret`, `authelia_client_password_hash`,
  `authelia_beszel_password_hash`, `authelia_oidc_rsa_key_content`. Don't edit the live
  file by hand — the next deploy overwrites it.
- Quirk preserved on purpose: the live HMAC secret value begins with a literal
  `Random Value: ` prefix (original generation never stripped the CLI banner). It's a
  valid key; changing it would invalidate outstanding OIDC tokens. Leave it.
- **users_database.yml stays FIRST-RUN-ONLY** — Authelia writes password changes back
  into it at runtime; re-templating would clobber user state.
- OIDC clients (e.g. Beszel) get their own secrets in `ansible/vars/secrets.yml`.
- **Built-in healthcheck:** the `authelia/authelia` image ships its own Docker
  `HEALTHCHECK` (a bundled `healthcheck.sh` that probes Authelia's internal health
  endpoint using its own binary), so Docker reports container health without a
  `healthcheck:` block in the compose template. It keeps working under `cap_drop: [ALL]`
  because it doesn't shell out to `curl`/`wget`. Monitoring (uptime-kuma) and `autoheal`
  can rely on this native status — don't add a redundant compose `healthcheck`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "authelia"`

## Fresh install
The role asserts the four generated secrets exist in SOPS before templating. On a
brand-new environment, generate them and add via `sops set` (values are JSON-encoded;
**strip the `Random Value: ` / `Digest: ` banner prefixes from new values**):
```bash
# OIDC HMAC secret -> authelia_oidc_hmac_secret
docker run --rm authelia/authelia:latest authelia crypto rand --length 64 --charset alphanumeric
# Per-client secret (give the plaintext to the app, store the digest)
#   -> authelia_client_password_hash (jellyfin), authelia_beszel_password_hash (beszel)
docker run --rm authelia/authelia:latest authelia crypto hash generate pbkdf2 \
  --variant sha512 --random --random.length 32 --random.charset alphanumeric
# RSA keypair -> authelia_oidc_rsa_key_content (full PEM incl. trailing newline)
docker run --rm -v "$PWD:/keys" authelia/authelia:latest authelia crypto pair rsa generate --directory /keys --bits 4096

sops set ansible/vars/secrets.yml '["authelia_oidc_hmac_secret"]' '"<value>"'
```
`private.pem`/`public.pem` in the config dir are vestigial on existing installs (the
key is sourced from SOPS) — kept as an offline copy, not read by the role.
