# Authelia re-templatable configuration — design

**Date:** 2026-06-11
**Status:** approved

## Problem

`ansible/roles/containers/authelia/templates/configuration.yml.j2` is rendered on
**first run only** — the generation block is guarded by `stat.exists` because the
render embeds values generated at setup time (OIDC HMAC secret, OIDC client password
hashes, RSA private key). Consequences:

- Template edits silently never reach the existing install. Confirmed drift as of
  today: totp issuer, `remember_me` (1w vs live 1M), regulation timings, the
  `*.local.*` access rule (template `one_factor`, live `bypass` — a hardening that
  never deployed), and the beszel OIDC client (active live, commented in template).
- Every real config change needs a hand-written idempotent `ansible.builtin.replace`
  migration task against the live file (pattern: "Migrate deprecated OIDC lifespan
  keys"). These compound and are fragile.
- Immediate motivator: raising `server.buffers.read` to 16384 to fix HTTP 431s on
  Pi-hole query-log forward-auth subrequests required yet another migration task.

## Decision

Make every input to `configuration.yml.j2` deterministic, then template it on
**every** deploy.

1. **Generated values move to SOPS** (`ansible/vars/secrets.yml`), one-time
   extraction from the live config — matching the repo convention for
   `authelia_jwt`/`authelia_secret`/`authelia_storage`:
   - `authelia_oidc_hmac_secret` — migrated **byte-exact**, including the
     pre-existing `Random Value: ` prefix left by the original generation task
     (stripping it would invalidate outstanding OIDC tokens; it is a valid key).
   - `authelia_client_password_hash` — jellyfin client digest.
   - `authelia_beszel_password_hash` — currently the same digest as jellyfin
     (live state); separate var so beszel can rotate independently later.
   - `authelia_oidc_rsa_key_content` — byte-exact content of
     `containers/authelia/config/private.pem`.
   Var names match the references already in the template.
2. **Role refactor:** delete the first-run generation block, the `stat` guard on
   configuration.yml, and both live-file migration tasks (lifespans, buffers).
   Add a fail-fast `assert` that the four SOPS vars are defined, then an
   unconditional `template` task (`mode 0600`, `become`, `no_log`) registered into
   `common_config_changed` so config edits recreate the container.
3. **Template reconciliation:** fold all live-file drift back into the template so
   the rendered output is byte-identical to the live file **except** the new
   `server.buffers.read: 16384` block. Intentional-looking template values that
   never deployed (`one_factor` LAN rule, totp issuer, regulation, remember_me) are
   reverted to live truth and may be re-applied deliberately afterwards.
4. **Unchanged:** `users_database.yml` stays first-run-only — Authelia writes
   password changes into it, so re-templating would clobber user state. The argon2
   `Hash password` docker-run moves under that guard (it is only needed when the
   user DB is first created).
5. **Fresh install path:** role `CLAUDE.md` documents generating the four values
   (`authelia crypto rand`, `authelia crypto hash generate pbkdf2`,
   `authelia crypto pair rsa generate`) and adding them via `sops set`. The
   on-disk `private.pem`/`public.pem` become vestigial for the role.

## Verification

- Out-of-band render of the template with real vars diffs against the live file:
  only the buffers block may differ.
- `deploy.yml --tags authelia --check` shows config change only; real deploy
  recreates the container; Authelia healthy; login + Jellyfin OIDC still work
  (no key material changed).
- Pi-hole query log loads; Authelia logs free of "exceeded the server read buffer";
  no 431s in Traefik access log for `/api/queries`.
- `prek run` green; commit directly to master.

## Rejected alternatives

- **State files in config dir, slurped each run** — keeps fresh installs fully
  automated but ties disaster recovery to Kopia backups of the config dir instead
  of git+SOPS (which now has off-box DR), and keeps generation tasks alive.
- **Authelia-native secret files / config filters** — upstream-blessed, but
  go-template `{{ }}` syntax collides with Jinja2 inside the `.j2`, and JWKS
  key-by-file handling is version-sensitive. More moving parts, same outcome.
