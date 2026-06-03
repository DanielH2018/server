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

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "cloudflare-ddns"`
