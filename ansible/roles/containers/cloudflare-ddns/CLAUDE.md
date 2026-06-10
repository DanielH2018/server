# cloudflare-ddns — Dynamic DNS updater

Keeps Cloudflare A/AAAA records pointed at the homelab's current public IP.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `favonia/cloudflare-ddns:latest`
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** monitoring
- **Depends on:** nothing
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Cloudflare API token comes from `ansible/vars/secrets.yml`.
- Pairs with Traefik's Cloudflare DNS-01 challenge for public TLS.
- **Heartbeat monitoring:** each updater pings an Uptime Kuma **push** monitor
  (`UPTIMEKUMA` env) after every successful update — a dead-man's-switch for silent
  failures, since favonia is distroless and can't carry a Docker healthcheck. The
  push monitors are created by AutoKuma via the `kuma(..., monitor_type='push',
  push_token=...)` macro branch; tokens are `cloudflare_ddns_{proxied,direct}_push_token`
  in `secrets.yml` (we set them — Kuma honors client-supplied tokens). Uses the
  internal `http://uptime-kuma:3001` URL (same `monitoring` net) to keep the
  heartbeat off public DNS and the Authelia-gated route.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "cloudflare-ddns"`
