# littlelink — Public link-in-bio landing page

The public "linktree"-style landing page. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `ghcr.io/techno-tim/littlelink-server:latest`
- **Host:** daniel-server · **Port:** 3000 · **URL:** `www.<domain>` (hostname override `www`)
- **Authelia:** **no — public by design** (it's the public landing page)
- **Networks:** apps
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- This is the one intentionally public, unauthenticated web surface (besides n8n's
  `/webhook/`). The `hostname: www` in `containers_list` is what routes it at the apex.
- Links/theme are set via environment variables in the compose.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "littlelink"`
