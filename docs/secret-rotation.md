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
| `auto` | 180 d | locally generated push tokens — no external coupling | `rotate --commit`, then redeploy the consumer |
| `assisted` | 365 d | app passwords / API keys / OIDC secrets | app-side step (below) |
| `external` | 365 d | provider-managed (Cloudflare/Discord/Mullvad/SMTP/LLM) | mint in the provider console |
| `pinned` | 730 d | **must not be naively swapped** | special procedure (below) |
| `ignore` | — | not a secret (domain, usernames, static addresses) | n/a |

Classification is by name in `scripts/secret_rotation.py`; override per-secret by editing
its `tier` in the registry (`sync` preserves overrides).

## `auto` — automated

`rotate --commit` generates a new 32-char token, writes it via `sops set`, and records the
date. Then redeploy whatever reads it, for example, `uv run ansible-playbook ansible/deploy.yml
--tags monitor-bridge`. Uptime Kuma honours the new push token on the next push — no Kuma
UI step. Because only **coming-due** secrets rotate (due within `ROTATE_LEAD_DAYS` = 8 —
one weekly cron interval, so a token rotates the Sunday *before* it goes overdue and the
daily audit never pages DOWN on a rotation the cron was about to do), runs stay staggered.

### Auto-rotate contract (the weekly cron changes secrets unattended)
The `rotate --commit` weekly cron is autonomous and state-changing — its authority, bounded
(harness-engineering's versioned-contract pattern for a change-producing role):
- **Scope / exclusions:** only `auto`-tier, locally generated push tokens with **no external
  coupling**, and only those **coming due** within `ROTATE_LEAD_DAYS` (8). `assisted` / `external` /
  `pinned` are **never** touched by the cron — a `pinned` swap loses data (see below).
- **Mode:** change-producing — writes via `sops set`, commits the registry, and the operator
  redeploys the consumer. Not report-only; there is no dry-run cron (the bare `rotate` is the dry run,
  run by hand).
- **Required evidence:** the daily "Secret Rotation" Kuma monitor must return green after a rotation.
  An `auto`-tier secret still overdue after a cron window means the **cron broke** — investigate it,
  don't hand-rotate around it.
- **Abort/escalation:** the cron only ever *narrows* to coming-due `auto` secrets; anything it can't
  classify stays untouched and surfaces in `audit`, never a blind rotate.

## `assisted` — app-issued (regenerate in the app, then update SOPS)

General shape: rotate/regenerate the credential **in the app**, `sops set
ansible/vars/secrets.yml '["<name>"]' '"<new>"'`, update the registry date (`sync` won't,
since the value already existed — set `last_rotated` by hand or re-run after editing), then
redeploy the app **and** every consumer (for example, Homepage, monitor-bridge, configarr). Examples:
- `*_api_key` (sonarr/radarr/jellyfin/prowlarr): Settings → General → regenerate API key.
- **CrowdSec bouncer keys** (`crowdsec_k8s_bouncer_api_key`, the cluster edge's — the only
  bouncer since E7 retired the Docker edge and its `dockertraefik` key) and the agent password
  (`crowdsec_k8s_agent_password`, shared by all four agents). Since slice-6 B2 the single
  LAPI lives in the cluster and registration is DECLARATIVE — the engine's `BOUNCER_KEY_*`
  env, not `cscli`. Rotation is therefore: `sops set` the new value → delete the old
  registration on the engine
  (`kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers delete k8straefik`;
  `cscli machines delete <name>` for the
  agent password) → redeploy `--tags crowdsec` on daniel-box → redeploy the consumers
  (`--tags traefik`/`--tags authelia` on daniel-box for the sidecars; the node-agent
  DaemonSet pods re-register themselves on the `crowdsec` redeploy). The delete is
  required because re-registration never
  UPDATES an existing key. **Do NOT just `sops set`**: the plugin hot-reloads the new key
  while the LAPI still holds the old hash, and it fails OPEN — a silent WAF bypass. The
  traefik deploy now fails loudly on a rejected key (its probe task), which is the guard.
  Verify after: `kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers list`
  (fresh `last_pull`) and a `kubectl -n homelab logs deploy/traefik` free of LAPI 403s.
  (`crowdsec_bouncer_api_key` — the retired local LAPI's legacy key — and
  `crowdsec_bouncer_docker_traefik_key` were both RETIRED 2026-08-13 with the Docker edge.)
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

These encrypt/anchor existing data; swapping the value alone **loses data**.

### The safe-cutover discipline for any pinned rotation
A pinned secret anchors existing data, so treat rotation as a **staged cutover with a live fallback**,
never a swap (harness-engineering's consequential-operation state machine — verify the new path
before you destroy the old one):
1. **Back up the anchored data first** (snapshot `authelia/config/db.sqlite3` — a Longhorn
   snapshot of the authelia PVC, or `kubectl cp` the file out). This backup is the recovery
   path — keep it until step 5 passes.
2. **Re-key through the tool, not `sops set`** (`authelia storage encryption change-key`;
   retired instance: `kopia repository change-password`) so the data is re-anchored to the
   new value.
3. **Prove the new value opens the data BEFORE removing anything** (an Authelia login +
   TOTP). If this fails, the old artifact is still on disk and still works.
4. **Only then** `sops set` the new value and redeploy the consumer.
5. **Verify live** (audit resets green, Kuma monitor green, a real restore/login works) — and only
   after that delete the pre-rotation backup. If any step fails, anchored data + old value are intact.

The failure this prevents: `sops set` first, redeploy, discover the data is now undecryptable — and
the only value that could open it has already been overwritten. The two procedures below are concrete
instances of this discipline:

- **`kopia_password`** — REMOVED (8edb11cd, 2026-08-13). The kopia B2 repo it anchored was
  deleted after the first verified Longhorn-only nightly, per the retirement plan
  (`docs/archive/k3s-migration/backup-consolidation-longhorn.md`), and the residual hidden object
  versions were hard-purged 2026-08-14 — the value opens nothing anymore. Kept here as the
  worked example of a pinned secret leaving the registry: the anchored data is destroyed
  first, deliberately, and only then does the key go. (The `kopia_b2_*` credentials are NOT
  kopia's — they are the B2 account keys, still live as Longhorn's backup-target credential.)
- **`authelia_storage`** — the Authelia DB encryption key. Use Authelia's migration, never a
  raw swap (a raw swap makes the existing SQLite DB undecryptable → TOTP/sessions lost):
  ```bash
  kubectl -n homelab exec -it deploy/authelia -- authelia storage encryption change-key --help
  ```
  Back up the DB first — it lives at `/config/db.sqlite3` on the authelia Longhorn PVC
  (take a Longhorn snapshot, or copy the file out with `kubectl cp`).

After any rotation, run `audit` to confirm the secret's window resets (green), and watch the
"Secret Rotation" Kuma monitor.
