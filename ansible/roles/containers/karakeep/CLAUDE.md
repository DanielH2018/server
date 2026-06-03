# karakeep — Bookmark manager

Karakeep (formerly Hoarder) with several co-deployed helper containers.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `ghcr.io/karakeep-app/karakeep:release` + `alpine-chrome` (headless Chrome)
  + `getmeili/meilisearch` + a `uv`/TimeTagger container
- **Host:** daniel-server · **Port:** 3000 · **URL:** `karakeep.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Headless Chrome renders page snapshots; MeiliSearch powers full-text search; both are
  internal helpers, only Karakeep is routed by Traefik.
- App secrets (MeiliSearch master key, NextAuth secret, etc.) live in `ansible/vars/secrets.yml`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "karakeep"`
