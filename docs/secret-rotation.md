# Secret rotation — audit, tiers, and runbooks

Secrets in `ansible/vars/secrets.yml` are tracked for rotation by a plaintext registry
(`ansible/secret_rotation.yml`) and the tool `scripts/secrets_mgmt/secret_rotation.py`. A daily server
cron (`secret-rotation-audit.sh`, initial_setup) pushes the **"Secret Rotation"** Uptime
Kuma monitor — it goes **down** when any secret is past its per-tier window, or when a
secret exists in `secrets.yml` but not the registry.

Rotation dates are **staggered** at registration (a deterministic per-name offset), so the
~90 secrets come due a few at a time across the year — never all on one day.

## Daily use

```bash
uv run python scripts/secrets_mgmt/secret_rotation.py sync     # after adding/removing a secret — registers it
uv run python scripts/secrets_mgmt/secret_rotation.py audit    # what's due / overdue, by tier
uv run python scripts/secrets_mgmt/secret_rotation.py rotate            # dry-run: due auto-tier secrets
uv run python scripts/secrets_mgmt/secret_rotation.py rotate --commit   # actually rotate due auto secrets
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

Classification is by name in `scripts/secrets_mgmt/secret_rotation.py`; override per-secret by editing
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
  **Bazarr is a consumer of `sonarr_api_key` and `radarr_api_key` that no deploy reaches.** It
  stores both in its own config on the `bazarr-config` PVC, entered through its UI, so there is
  no Ansible template holding them and no redeploy that updates them. After rotating either key,
  set it in Bazarr → Settings → Sonarr → API Key and Settings → Radarr → API Key, then restart
  Bazarr from System → Restart. The restart is required, not cosmetic: Bazarr's SignalR client
  raises `UnAuthorizedHubError` and its thread exits, so it does not retry on a key change alone.
  Missing this on 2026-08-29 left Bazarr reconnecting in a loop that leaked 173 MiB → its 1Gi cap
  in 90 minutes and OOM-killed it; the only visible signal was the "k3s Container OOM" monitor,
  which self-clears one hour after the kill while subtitle fetching stays silently broken.
- **CrowdSec bouncer keys** (`crowdsec_k8s_bouncer_api_key`, the cluster edge's — the only
  bouncer since E7 retired the Docker edge and its `dockertraefik` key) and the agent password
  (`crowdsec_k8s_agent_password`, shared by all four agents). Since slice-6 B2 the single
  LAPI lives in the cluster. Bouncer registration is DECLARATIVE — the engine reads
  `BOUNCER_KEY_*` from its env rather than running `cscli`. Agent registration is not: the role
  runs `cscli machines add`, and that task adds a machine only when it is absent. It passes no
  `--force` and has no update path, so a rotation that skips the deletes leaves every machine on
  the old password. Rotation is therefore: `sops set` the new value → delete the old
  registrations on the engine → redeploy `--tags crowdsec` on daniel-box → redeploy the
  consumers (`--tags traefik`/`--tags authelia` on daniel-box for the sidecars). The delete is
  required because re-registration never UPDATES an existing key. For the bouncer key that is
  one command:
  `kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers delete k8straefik`.
  For the agent password it is one `cscli machines delete <name>` per machine, and there are
  four: `k8s-traefik-agent`, `k8s-authelia-agent`, `k8s-node-agent-daniel-box` and
  `k8s-node-agent-daniel-server`. The last two are per-node and come from
  `crowdsec_k8s_node_agent_machines` in the role's `defaults/main.yml`; a node joining the
  cluster adds a name there. The `--tags crowdsec` redeploy restarts the node-agent DaemonSet,
  because the role names `crowdsec-node-agent` in `manifests_extra_rollouts`. That restart is
  what rotates them: `AGENT_PASSWORD` arrives through a `secretKeyRef`, and env resolves once at
  pod start, so only a new pod reads the new value. Expect the node agents to fail their LAPI
  login for a few minutes in the middle of that deploy. The restart fires before the
  re-registration task, so the machines are deleted and not yet re-added. They converge without
  help: each agent's `livenessProbe` runs `cscli lapi status` every 60s and restarts the
  container until registration lands. **Do NOT just `sops set`**: the plugin hot-reloads the new key
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
3. **Prove the new value opens the data BEFORE overwriting the old one.** **Whether you still
   hold a live fallback here depends on the tool, so check before you rely on one.** Some
   re-key tools leave the original artifact intact, and the old value still opens it.
   Authelia's `change-key` does not: it re-encrypts the SQLite database **in place**, so the
   instant it succeeds the old key opens nothing and the step-1 backup is the only way back.
   Assume in-place unless the tool documents otherwise.
4. **Only then** `sops set` the new value and redeploy the consumer.
5. **Verify live** (audit resets green, Kuma monitor green, a real restore/login works) — and only
   after that delete the pre-rotation backup. If any step fails, restore the step-1 backup;
   see step 3 for why the old value itself may no longer be a fallback.

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
- **`authelia_storage`** — the Authelia DB encryption key. It encrypts the TOTP secrets and
  WebAuthn credentials in `/config/db.sqlite3` on the authelia Longhorn PVC. A raw swap makes
  that database undecryptable, and code-server, n8n and longhorn are `two_factor` — so a
  botched rotation locks those three behind a challenge nobody can satisfy until every
  enrolment is redone. Snapshot the PVC in the Longhorn UI first.

  The container reads the current key from its mounted config, so only the **new** value ever
  reaches a command line. Run these on daniel-box, and **not from a Claude Code session** — a
  bash-input is transcribed, and a transcribed key is the exposure this file exists to prevent:

  ```bash
  NEW=$(openssl rand -hex 32)
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption change-key -c /secrets/configuration.yml --new-encryption-key "$NEW"

  # must SUCCEED — the new key opens the data
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption check -c /secrets/configuration.yml --encryption-key "$NEW"

  # must FAIL — the config still carries the old key, which should now open nothing
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption check -c /secrets/configuration.yml
  ```

  Take both halves of that pair. A `check` observed only succeeding does not show that
  `change-key` did anything; the second command failing is the evidence that it did.

  Between `change-key` and the redeploy, the database is on the new key while the running pod's
  config still carries the old one, so TOTP verification is broken. Keep the gap short. The
  redeploy is also a real SSO outage for everything behind the Authelia middleware, because the
  Deployment is `replicas: 1` with `strategy: Recreate`.

  Verify with a **TOTP challenge, not a login** — a password login succeeds without the storage
  key ever being read.

After any rotation, run `audit` to confirm the secret's window resets (green), and watch the
"Secret Rotation" Kuma monitor.
