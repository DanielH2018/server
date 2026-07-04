# karakeep — Bookmark manager

Karakeep (formerly Hoarder) with several co-deployed helper containers.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `ghcr.io/karakeep-app/karakeep:release` **pinned by digest** + `alpine-chrome`
  (headless Chrome) + `getmeili/meilisearch` + a `uv`/TimeTagger container
- **Host:** daniel-server · **Port:** 3000 · **URL:** `karakeep.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Headless Chrome renders page snapshots; MeiliSearch powers full-text search; both are
  internal helpers, only Karakeep is routed by Traefik.
- App secrets (MeiliSearch master key, NextAuth secret, etc.) live in `ansible/vars/secrets.yml`.
- **The karakeep app image is digest-pinned + manual-update.** `:release` is a mutable floating
  tag (no semver for Renovate to order) and Compose never re-pulls a present tag, so a plain
  redeploy leaves it stale with no signal — hence the digest pin (same pattern as tdarr/janitorr).
  **To update:** `docker pull ghcr.io/karakeep-app/karakeep:release`, take the new digest, update
  the `image:` line, redeploy — checking the meili VERSION POLICY still holds for the new version.
- **MeiliSearch upgrades are manual** (pinned; its own no-automerge Renovate rule) — minor
  versions change the on-disk DB format and refuse to boot on an old database.
- `/app/apps/web/.next/cache` is a **tmpfs** (256M): the image dir is node-owned but the
  container runs as root with `cap_drop: ALL` (no DAC_OVERRIDE), so thumbnail-cache writes
  EACCESed without it.
- **time-tagger healthcheck:** the loop touches `/tmp/healthy` only on a *successful*
  tagger run; the healthcheck (mtime < ~2 cycles) flags hung or persistently failing runs
  and autoheal restarts. `start_period: 600s` covers the `uv pip install` + first run.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "karakeep"`
