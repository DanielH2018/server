# SPEC — Self-host `slopus/happy` (standalone) for unified cross-machine Claude Code sessions

**Status:** ready to execute in a fresh session. Self-contained — the executor has none of the
planning conversation. Supersedes `docs/happy-server-selfhost-handoff.md` where they conflict
(see **§2 Corrections to the handoff brief** — the brief predates reading current upstream source).

---

## 1. Objective & locked decisions

Stand up a self-hosted **happy** server on **daniel-server** so Claude Code sessions on Daniel's
Windows PC and on the homelab appear in one merged, switchable, **end-to-end-encrypted** session
list (phone native app + browser). Replaces the vendor cloud relay (`api.cluster-fluster.com`).
The server never sees plaintext — preserve that.

Decisions locked during planning (do not re-litigate):

| Decision | Choice | Rationale |
|---|---|---|
| **Host** | `daniel-server` | Pi is a 512 MB Zero 2 W — can't build/run a node stack. |
| **Architecture** | **Standalone single container** (root `Dockerfile` → `sources/standalone.ts`) | Embedded PGlite Postgres + local-FS blobs + in-memory event bus. No Redis, no MinIO, no external Postgres. Fully delivers a merged session list for a 2-machine personal setup. |
| **Exposure** | **LAN/WireGuard-only** | Route only `happy.local.{{ domain }}` (Pi-hole DNS over WG/LAN). **No** public Cloudflare router. Gate = network reachability + happy's native E2E device-auth. |
| **Auth in front** | **None** (no Authelia, no static bearer) on the sync route | happy clients authenticate with happy's own crypto pairing; they can't send a Traefik bearer or pass Authelia 2FA. `.local`-only + E2E is the posture. |
| **Webapp** | **Staged** — Phase 3, after server+phone+PC validation | Not bundled by the standalone image; separate static build (see §6). |
| **Secrets** | SOPS via `/add-secret` | Never a plaintext `.env`. |
| **Repo path** | new role `ansible/roles/containers/happy/` | Resolves the brief's `<HOMELAB_REPO_PATH>`. |
| **Domain** | the SOPS-encrypted `{{ domain }}` | Resolves the brief's `<HOMELAB_DOMAIN>`. |

---

## 2. Corrections to the handoff brief (verified against `slopus/happy` @ `main`, 2026-07-19)

The brief describes the repo's **"full" Kubernetes deployment shape** (`Dockerfile.server` + external
Postgres/Redis/MinIO). Current source shows a **standalone shape** that we're using instead. Deltas
the executor MUST NOT follow from the brief:

- **Redis and MinIO are NOT required.** Both are gated on `REDIS_URL` / `S3_HOST` being set
  (`sources/main.ts`; `sources/storage/files.ts:5` `useLocalStorage = !process.env.S3_HOST`). The
  brief's and `docs/deployment.md`'s "required" framing is stale. **Do not deploy Postgres, Redis,
  or MinIO.** This dissolves the brief's open items #2 (MinIO reachability) and #3 (Redis/S3 necessity).
- **Package manager is pnpm**, not yarn. Root `Dockerfile` uses `corepack prepare pnpm@10.11.0` +
  `pnpm install --frozen-lockfile`.
- **Migrations are `tsx sources/standalone.ts migrate`** (hand-replayed SQL against PGlite), **not**
  `yarn prisma migrate deploy`. The image's `CMD` already runs migrate-then-serve; no separate
  migrate step is needed.
- **The webapp is a separate static build.** The root `Dockerfile` builds only `happy-wire` +
  `happy-server` and copies them into the runtime — it never runs `expo export` for `happy-app`, so
  the standalone container serves **API/WebSocket only** (no browser dashboard bundled). Webapp =
  Phase 3 (§6). This dissolves brief open item #1 (runtime-vs-build-time) as moot for Phase 1–2.
- **Required env is minimal:** `HANDY_MASTER_SECRET` (server throws without it) + a `/data` volume.
  Master-secret var name is `HANDY_MASTER_SECRET` (confirmed in `encrypt.ts`, `auth.ts`,
  `standalone.ts`).
- **`EXPOSE 3000` in `Dockerfile.server` is cosmetic/stale;** the standalone `Dockerfile` correctly
  `EXPOSE 3005` and the app's default `PORT` is `3005`.
- **CLI package is `happy`** (renamed from `happy-coder`); overrides the cloud default via
  `HAPPY_SERVER_URL` env or `~/.happy/settings.json` `serverUrl`.

---

## 3. Architecture (as-built target)

```
Windows PC  ──HAPPY_SERVER_URL──┐
Homelab CLI ──HAPPY_SERVER_URL──┤   (WireGuard / LAN, Pi-hole DNS)
Phone app   ──server URL────────┼──▶ https://happy.local.{{ domain }}
                                │        │ Traefik (TLS, wildcard cert), rate-limit@file
                                │        │ LAN-only router in file provider (no public DNS)
                                ▼        ▼
                          daniel-server  happy  (one container, :3005)
                                           ├─ Fastify + Socket.IO (sync backbone + WS relay)
                                           ├─ PGlite (embedded Postgres)   → /data/pglite
                                           └─ local-FS blob storage        → /data/files
                                         bind mount ./data:/data  (Kopia-backed)
```

- Every device points at `https://happy.local.{{ domain }}`; the server merges their session lists.
- E2E encryption: server stores only ciphertext. Do **not** set
  `DANGEROUSLY_LOG_TO_SERVER_FOR_AI_AUTO_DEBUGGING`; add no plaintext logging.

---

## 4. Files to create / modify

New role `ansible/roles/containers/happy/` — model it on `roles/containers/homelab-mcp/` (custom-built,
LAN-only, route in the traefik file provider). **Build-context difference:** homelab-mcp vendors a
tiny app; happy's `Dockerfile` needs the whole monorepo as context → check out the pinned upstream
repo on the host and build from it (§5 decides the exact mechanism — the one genuinely-open call).

| Path | Action | Notes |
|---|---|---|
| `ansible/roles/containers/happy/tasks/main.yml` | create | (1) `ansible.builtin.git:` checkout of `slopus/happy` at a pinned `{{ happy_git_version }}` into a gitignored host dir; (2) `include_role: common tasks_from: docker_deploy` (templates compose + builds); (3) optional weekly rebuild cron (base refresh), like homelab-mcp `tasks/main.yml:39`. |
| `ansible/roles/containers/happy/templates/docker-compose.yml.j2` | create | Single `happy` service. `build: {context: <checkout>, dockerfile: Dockerfile, pull: true}`. Env, `./data:/data` bind mount, healthcheck, `proxy` network, resources, minimal Traefik labels (publish service only — route lives in file provider). Skeleton in §7. |
| `ansible/roles/containers/happy/meta/main.yml`, `meta/deps.yml` | create | `dependencies: traefik` (mirror homelab-mcp). |
| `ansible/roles/containers/happy/CLAUDE.md` | create | Role doc: standalone shape, LAN-only, `/data` Kopia-backed, how to bump `happy_git_version`, no Authelia/bearer + why. |
| `ansible/inventory/host_vars/daniel-server.yml` | modify | Add to `containers_list`: `{name: happy, port: 3005, use_authelia: false, networks: [proxy]}`. Add vars: `happy_git_version` (pinned ref), resource caps if parameterized. |
| `ansible/roles/containers/traefik/templates/config.yml.j2` | modify | Add a `happy` router: `Host(\`happy.local.{{ domain }}\`)` → `happy@docker`, `entryPoints: [https]`, `middlewares: [rate-limit]`, `tls` wildcard. **No public `happy.{{ domain }}` router, no bearer header match** (unlike homelab-mcp — clients use native auth). Model on the `homelab-mcp` block at `config.yml.j2:43`. |
| `ansible/vars/secrets.yml` | modify (via `/add-secret`) | `handy_master_secret` = `openssl rand -hex 32`. Then `uv run python scripts/secret_rotation.py sync` + commit. |
| `.gitignore` | modify | Ignore the happy source checkout dir under `containers/happy/`. |

---

## 5. OPEN ITEM to settle at execution start — build-context mechanism

There is **no published happy image**, so we must build. The root `Dockerfile` COPYs the whole
monorepo (`package.json`, `pnpm-lock.yaml`, `packages/*`, `scripts`, `patches`). Pick how the build
context gets onto the host (recommend the first):

1. **Ansible `git:` checkout at a pinned ref** into `containers/happy/src` (gitignored), compose
   `build.context: ./src`. Pin `happy_git_version` to a **release tag** if one exists, else a branch
   + explicit SHA. Bump the var to update. *Recommended* — reviewable pin, no repo bloat, satisfies
   "pin all versions."
2. Git submodule of `slopus/happy` (precedent: the email-to-rss CF-worker submodule). Heavier — a
   large monorepo in the homelab repo tree.
3. Build out-of-band, load the tagged image, reference it. Most manual; loses IaC reproducibility.

Confirm a concrete pinned ref exists (`gh api repos/slopus/happy/tags`) before writing the task.

---

## 6. Phase 3 — webapp (browser dashboard), after Phase 1–2 pass

Not bundled by the standalone image. Two build options; decide when we reach it:

- **A (recommended): separate static nginx container** from the repo's `Dockerfile.webapp` — build
  `happy-app`'s `expo export --platform web` with **build-time** `EXPO_PUBLIC_HAPPY_SERVER_URL=`
  `https://happy.local.{{ domain }}` baked in, served by nginx. Add a second `happy-webapp` service
  to the same role's compose + a Traefik route `app.local.{{ domain }}` → `:80`. The webapp is a
  browser flow, so it **can** carry Authelia — recommend `use_authelia`-style gating on the `app.`
  route as a defense-in-depth layer (it only gates the static UI; the API stays on its own route).
- **B: extend the standalone `Dockerfile`** to also `expo export` the webapp into `HAPPY_STATIC_DIR`
  so the one container serves it and self-injects `window.__HAPPY_CONFIG__ = {serverUrl:
  location.origin}` at runtime. Fewer containers, but a heavier/custom build.

Phone dashboard needs **no** webapp — it's the native happy mobile app pointed at the server URL.

---

## 7. Compose skeleton (Phase 1 — for reference, verify against shared macros at build)

```yaml
{% raw %}{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
{% from 'healthcheck.yml.j2' import healthcheck %}
{% from 'autokuma.yml.j2' import labels as kuma with context %}{% endraw %}
---
services:
  happy:
    build:
      context: ./src          # pinned checkout (see §5)
      dockerfile: Dockerfile
      pull: true              # refresh node:20 base on rebuild
    container_name: happy
    restart: unless-stopped
    environment:
      - HANDY_MASTER_SECRET={% raw %}{{ handy_master_secret }}{% endraw %}
      - PORT=3005
      - HOST=0.0.0.0
      - PUBLIC_URL=https://happy.local.{% raw %}{{ domain }}{% endraw %}   # attachment URLs must resolve over WG
      - NODE_ENV=production
      - TZ=America/Chicago
    volumes:
      - ./data:/data          # PGlite (/data/pglite) + blobs (/data/files); Kopia-backed bind mount
    # security hardening — verify happy tolerates it (node build; likely NOT read_only due to /data + tsx)
    security_opt: [no-new-privileges:true]
    {% raw %}{{ healthcheck('["CMD", "curl", "-f", "http://localhost:3005/health"]', start_period='40s', name='happy') }}{% endraw %}
    {% raw %}{{ service_networks() }}{% endraw %}
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network={% raw %}{{ (container_item.networks | default([docker_network]))[0] }}{% endraw %}"
      - "traefik.http.services.happy.loadbalancer.server.port={% raw %}{{ container_item.port }}{% endraw %}"
      - "com.centurylinklabs.watchtower.enable=false"   # built image, not a pulled tag
      {% raw %}{{ kuma('happy') }}{% endraw %}
    {% raw %}{{ resources('1.0', '1G', '0.10', '128M') }}{% endraw %}   # node+PGlite; tune from cAdvisor/Grafana

{% raw %}{{ external_networks() }}{% endraw %}
```

Notes: `curl` is present in the runtime image (`apt-get install ffmpeg curl`). Confirm `GET /health`
exists in standalone mode (brief claims it; verify in `sources/standalone.ts`). `cap_drop: [ALL]`
and `read_only: true` are aspirational — a `tsx`/node runtime writing under `/repo` + `/data` likely
won't tolerate them unmodified; add incrementally and verify, don't assume.

---

## 8. Staged build plan & per-phase verification

**Phase 0 — secrets & pin.** `/add-secret` `handy_master_secret`; `secret_rotation.py sync`; commit.
Pick + record `happy_git_version` (§5). *Verify:* `sops -d ansible/vars/secrets.yml | grep -q
handy_master_secret`.

**Phase 1 — server up.** Create the role + host_vars entry + traefik route. Deploy:
`uv run ansible-playbook ansible/deploy.yml --tags "traefik,happy"` (traefik tag re-renders the
file-provider route). *Verify (gating):*
`uv run python scripts/probe.py health happy` → running+healthy; and
`uv run python scripts/probe.py cert happy.local.{{ domain }}` / a curl of `/health` over the route
returns 200. Confirm no public `happy.{{ domain }}` router resolves.

**Phase 2 — merged session list (the real acceptance test).** Client wiring is Daniel's to run
(§9). *Verify (end-to-end, gating the whole effort):* with `HAPPY_SERVER_URL` set on **both** the PC
and daniel-server, `happy claude` on each; a session started on one machine appears in the other's
list and in the phone app — **and** a network capture / the CLI's own logs show traffic to
`happy.local.{{ domain }}` with **zero** connections to `api.cluster-fluster.com` (E2E self-relay
proven). Also confirm `happy claude` on the homelab inherits Daniel's `~/.claude` hooks + MCP config
(the brief's last open client-side unknown).

**Phase 3 — webapp (§6).** *Verify:* `app.local.{{ domain }}` loads the dashboard in a browser over
WG and shows the same merged list.

---

## 9. Client wiring (report to Daniel — do NOT execute from the deploy session)

```bash
# Each machine (PC + daniel-server):
export HAPPY_SERVER_URL=https://happy.local.<domain>
# then:
happy claude
```
- Defaults being overridden: `api.cluster-fluster.com` (server) / `app.happy.engineering` (webapp).
- The homelab leg needs **node + `claude` on PATH** on daniel-server — verify before Phase 2.
- Phone: point the native happy app's server URL at `https://happy.local.<domain>` (must be on
  WireGuard, which already carries Pi-hole DNS so `*.local` resolves).

---

## 10. Out of scope

- Postgres / Redis / MinIO containers (not needed — §2).
- Public-internet exposure, Authelia/bearer on the **sync** route (LAN/WG-only + native auth).
- Configuring the Windows PC or the phone from the deploy session (Daniel does those).
- Voice (`ELEVENLABS_API_KEY`), GitHub App, RevenueCat/E2B billing/sandbox integrations.
- Renovate automation of `happy_git_version` (nice-to-have; note it, don't build it here).

## 11. End-to-end verification (single gate for "done")

`probe.py health happy` green **AND** a session created on the PC shows up on daniel-server and the
phone via `happy.local.{{ domain }}`, with **no** traffic to `api.cluster-fluster.com`. Phase 3 adds:
`app.local.{{ domain }}` shows the same list in a browser.

## 12. Open items to confirm during build (flag, don't guess)

1. Concrete pinned upstream ref exists (`gh api repos/slopus/happy/tags`) — §5.
2. `GET /health` is served in standalone mode (`sources/standalone.ts`) — else adjust the healthcheck.
3. How much hardening (`cap_drop`, `read_only`) the node/tsx runtime tolerates — §7.
4. First `pnpm install` pulls the whole workspace incl. `happy-app`'s expo deps — build may be
   large/slow on first run; confirm disk headroom on daniel-server.
