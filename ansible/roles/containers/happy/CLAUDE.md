# happy — self-hosted Claude Code session-sync server

Standalone build of [`slopus/happy`](https://github.com/slopus/happy) — merges Claude Code
sessions from Daniel's Windows PC, this homelab, and the phone app into one switchable,
end-to-end-encrypted session list. Replaces the vendor cloud relay
(`api.cluster-fluster.com`). See `docs/happy-selfhost-spec.md` for the full design and
phased rollout; this file only covers what's specific to running the role.

## At a glance
- **Image:** built from a pinned upstream checkout (no published image exists) — see
  "Build context" below
- **Host:** daniel-server · **Port:** `3005` (internal; routed only via Traefik)
- **Authelia:** **no** — happy clients authenticate with their own E2E device-auth crypto
  pairing; they can't send a Traefik bearer or pass a 2FA redirect. Gated by network
  reachability instead (WireGuard/LAN + Pi-hole `*.local` DNS) — mirrors the reasoning
  used for `home-assistant`/`livesync`'s own-auth exemptions, not the `homelab-mcp` bearer
  pattern (there's no bearer here at all, since a happy client couldn't present one).
- **Networks:** `proxy` (Traefik reaches the route here)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list` +
  `happy_git_version`

## Architecture (standalone shape)
One container. No Postgres/Redis/MinIO — the standalone build embeds PGlite (Postgres) and
local-filesystem blob storage, both under the `./data` bind mount (`/data/pglite`,
`/data/files`), Kopia-backed. `HANDY_MASTER_SECRET` (SOPS, added via `/add-secret`) is
required — the server throws on boot without it. The server stores only ciphertext; do not
set `DANGEROUSLY_LOG_TO_SERVER_FOR_AI_AUTO_DEBUGGING` or add plaintext logging.

## Build context — pinned upstream checkout
There is no published happy image, and the root `Dockerfile` needs the whole pnpm monorepo
as build context (`package.json`, `pnpm-lock.yaml`, every `packages/*`). `tasks/main.yml`
does an `ansible.builtin.git` checkout of `slopus/happy` at `happy_git_version`
(`host_vars/daniel-server.yml`) into `containers/happy/src` — gitignored (everything under
`containers/` is outside the repo's tracked tree), compose builds `context: ./src`.

**To bump the version:** update `happy_git_version` and redeploy. The changed checkout
content shifts the Docker build layer cache, producing a new image ID, which
`docker_deploy.yml`'s `recreate: auto` already picks up (same as any image-tag bump) — no
`common_config_changed` wiring needed.

**No release tag currently covers the standalone shape.** The newest tag (`v3`) predates
`sources/standalone.ts`/PGlite (verified missing from that ref, 2026-07-19); the `cli-*`
tags are the npm `happy` CLI package's own releases, not the server. `happy_git_version` is
therefore pinned to an explicit `main` commit SHA. Re-check `gh api repos/slopus/happy/tags`
next time this is bumped — a real server release tag may exist by then.

**First build is slow and disk-heavy:** `pnpm install` (no `--filter`) resolves the entire
workspace declared in `pnpm-workspace.yaml`, including `happy-app`'s Expo/React Native
deps, even though only `happy-wire` + `happy-server` get built into the runtime image.
Confirm disk headroom before a version bump.

## Hardening
`cap_drop: [ALL]` + `no-new-privileges:true` (fleet baseline, enforced by
`validate-compose`). **Runs as root** (image default): the Dockerfile sets no `USER`, and a
compose `user: "1000:1000"` override does NOT work here — upstream bakes the app tree under
`/repo` root-readable-only, so uid 1000 can't even resolve `sources/standalone.ts`
(`ERR_MODULE_NOT_FOUND`). So the container stays root, and `tasks/main.yml` makes the
`./data` bind mount **root-owned** — otherwise, under `cap_drop:[ALL]` (no
`CAP_DAC_OVERRIDE`), root can't `mkdir /data/pglite` in the 1000-owned dir
(`EACCES`). Both failure modes were hit + resolved on the 2026-07-19 first deploy; this
matches the `root-container-secret-file-must-be-root-owned` pattern (fix ownership to the
container's uid, don't fight a root image). `read_only: true` is **not** set: a `tsx`/node
runtime may write a compile cache under `/repo`; revisit with a scoped `tmpfs:` if hardening
further. Root + `cap_drop:[ALL]` + `no-new-privileges` is an accepted fleet posture for
root-designed images (cf. portainer, zigbee2mqtt).

## Daemon service (host-side, not the container)

Separate from the server container: this host is also a **client**. The `happy` CLI
(`npm i -g happy`, fnm node) runs a background **daemon** that keeps daniel-server reachable
from the phone/PC — view, resume, and spawn Claude Code sessions here without a terminal
open. To survive reboots it's wrapped in a systemd **timer + oneshot** (`happy-daemon.timer`
→ `happy-daemon.service`, templated to `/etc/systemd/system`): boot + every 5 min it runs
`happy daemon start`, which is **idempotent** (no-op if already up), as `User=ubuntu`. **No
lingering** — it's a system unit, not a user-session service. `node`/happy are invoked by
absolute path via fnm's `default` alias, so an fnm node-version bump keeps working **only if
`happy` is re-installed globally on the new version** (fnm global npm packages don't carry
across versions). Pairing (`~/.happy` token) persists across restarts — no re-scan.
- Check: `systemctl status happy-daemon.timer` · `happy daemon status` · `happy daemon list`
- The phone/PC set the server via the app's **Custom Server URL** setting (`server.tsx`),
  NOT the "Authenticate Terminal → paste URL" field (which wants the CLI's QR/pairing URL —
  pasting the server URL there yields "Invalid Authentication URL").

## Webapp — browser dashboard (Phase 3, deployed)
Second service `happy-webapp` in this role's compose: a separate static build
(`Dockerfile.webapp` — `expo export --platform web` → `nginx:alpine` on :80), routed at
`app.local.<domain>` (traefik file provider). The standalone server image serves
API/WebSocket only, so this is its own container. Not needed for the phone app or CLI.
- **Server URL is baked at build time.** Compose passes `build.args.HAPPY_SERVER_URL` →
  the Dockerfile maps it to `ENV EXPO_PUBLIC_HAPPY_SERVER_URL`, which `getServerUrl()`
  (`packages/happy-app/sources/sync/serverConfig.ts`) falls back to before the cloud default.
  So the dashboard defaults to `https://happy.local.<domain>` out of the box — no manual
  "Custom Server URL" needed (that step was only for the phone's *prebuilt* vendor app, which
  had no bake). Bumping `happy_git_version` rebuilds both services and re-bakes this.
- **Cross-origin is fine.** The browser (origin `app.local`) calls the API at `happy.local` —
  the server sets Fastify + Socket.IO CORS `origin:'*'`, and the vendor itself runs the webapp
  on a separate origin, so this is a first-class config.
- **No Authelia (WG-only), by decision.** Same posture as the sync route: the bundle holds no
  secrets and its API calls hit `happy.local` (also un-Authelia'd), so a UI-only gate would add
  a second login for no data-protection gain. Reachability + happy's native auth is the gate.
- **Hardening:** `nginx:alpine` binds :80, so under `cap_drop:[ALL]` it needs
  `cap_add: [CHOWN, SETUID, SETGID, NET_BIND_SERVICE]` (the freshrss `feed-cache` twin).
  Healthcheck hits `http://127.0.0.1/` — **not** `localhost` (busybox `wget` resolves that to
  `::1`, which nginx's IPv4-only `listen 80` refuses). `read_only` is deferred (matches the
  server service); revisit with a `tmpfs` if hardening further.
- **First build is heavy:** the webapp's `pnpm install --filter happy-app...` pulls the whole
  Expo/React Native toolchain — separate from and heavier than the server build. Cached after.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Checkout/cron: `tasks/main.yml` ·
  Route: `roles/containers/traefik/templates/config.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "traefik,happy"`
  (traefik tag re-renders the file-provider route)
- Health: `uv run python scripts/probe.py health happy`
