# Secret rotation — audit, tiers, and runbooks

Secrets in `ansible/vars/secrets.yml` are tracked for rotation by a plaintext registry
(`ansible/secret_rotation.yml`) and the tool `scripts/secret_rotation.py`. A daily server
cron (`secret-rotation-audit.sh`, initial_setup) pushes the **"Secret Rotation"** Uptime
Kuma monitor — it goes **down** when any secret is past its per-tier window, or when a
secret exists in `secrets.yml` but not the registry.

Rotation dates are **staggered** at registration (a deterministic per-name offset), so the
~90 secrets come due a few at a time across the year — never all on one day.

## Daily use

```bash
uv run python scripts/secret_rotation.py sync     # after adding/removing a secret — registers it
uv run python scripts/secret_rotation.py audit    # what's due / overdue, by tier
uv run python scripts/secret_rotation.py rotate            # dry-run: due auto-tier secrets
uv run python scripts/secret_rotation.py rotate --commit   # actually rotate due auto secrets
```

`sync` edits the (git-tracked) registry — **commit it**. The audit cron never writes the
registry, so git stays the source of truth.

## Tiers

| Tier | Cadence | What it is | Rotation |
|------|---------|-----------|----------|
| `auto` | 180 d | locally-generated push tokens — no external coupling | `rotate --commit`, then redeploy the consumer |
| `assisted` | 365 d | app passwords / API keys / OIDC secrets | app-side step (below) |
| `external` | 365 d | provider-managed (Cloudflare/Discord/Mullvad/SMTP/LLM) | mint in the provider console |
| `pinned` | 730 d | **must not be naively swapped** | special procedure (below) |
| `ignore` | — | not a secret (domain, usernames, static addresses) | n/a |

Classification is by name in `scripts/secret_rotation.py`; override per-secret by editing
its `tier` in the registry (`sync` preserves overrides).

## `auto` — automated

`rotate --commit` generates a new 32-char token, writes it via `sops set`, and records the
date. Then redeploy whatever reads it, e.g. `uv run ansible-playbook ansible/deploy.yml
--tags monitor-bridge`. Uptime Kuma honours the new push token on the next push — no Kuma
UI step. Because only **due** secrets rotate, runs stay staggered.

## `assisted` — app-issued (regenerate in the app, then update SOPS)

General shape: rotate/regenerate the credential **in the app**, `sops set
ansible/vars/secrets.yml '["<name>"]' '"<new>"'`, update the registry date (`sync` won't,
since the value already existed — set `last_rotated` by hand or re-run after editing), then
redeploy the app **and** every consumer (e.g. Homepage, monitor-bridge, recyclarr). Examples:
- `*_api_key` (sonarr/radarr/jellyfin/prowlarr): Settings → General → regenerate API key.
- `crowdsec_bouncer_api_key`: generate any new 32+ char value, `sops set` it, then redeploy
  traefik (`--tags traefik`). The role's registration flow is rotation-safe: it probes LAPI
  with the configured key and, on mismatch, deletes + re-adds `traefik-bouncer` (cscli has
  no update flag), then restarts traefik. **Do NOT just `sops set` without the redeploy** —
  traefik hot-reloads the new key from the file provider while LAPI still holds the old
  hash, and the bouncer plugin fails OPEN (silent WAF bypass) until re-registration.
  Verify after: `docker exec crowdsec cscli bouncers list` (fresh `last_pull` on
  `traefik-bouncer`/its `@<ip>` row) and a `docker logs traefik` free of LAPI 403s.
- `grafana_admin_password`, `*_password`: change in the app (or its env on first run).
- `authelia_secret` / `authelia_jwt`: rotating forces all users to re-login (not breaking).
- `authelia_oidc_hmac_secret` / `*_password_hash`: re-issues OIDC — re-pair jellyfin (the
  live OIDC client; beszel's client is provisioned but parked in `archive/`, re-pair only
  if reactivated).

## `external` — provider consoles (audit-only)

Mint a new value in the provider, then `sops set` + redeploy the consumer:
- `cloudflare_dns_token`: Cloudflare dashboard → API Tokens (keep it **zone-scoped**: DNS
  edit + Zone read for the one zone — audit this scope when rotating).
- `*_discord_webhook*`: Discord → channel → Integrations → Webhooks → regenerate.
- `mullvad_account`, `wireguard_peer_*`: Mullvad account panel / regenerate the WG key.

## `pinned` — DANGER, never `sops set` blindly

These encrypt/anchor existing data; swapping the value alone **loses data**:

- **`kopia_password`** — the backup repository password. Change it *through Kopia* or you
  can no longer open the repo (all backups become unreadable):
  ```bash
  docker exec -it kopia kopia repository change-password
  ```
  then `sops set` the new value and redeploy kopia. Verify a `kopia snapshot list` works.
- **`authelia_storage`** — the Authelia DB encryption key. Use Authelia's migration, never a
  raw swap (a raw swap makes the existing SQLite DB undecryptable → TOTP/sessions lost):
  ```bash
  docker exec -it authelia authelia storage encryption change-key --help
  ```
  Back up `containers/authelia/config/db.sqlite3` first.

After any rotation, run `audit` to confirm the secret's window resets (green), and watch the
"Secret Rotation" Kuma monitor.
