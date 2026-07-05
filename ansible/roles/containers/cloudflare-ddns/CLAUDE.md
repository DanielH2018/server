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
- **This role manages ONLY `{{ domain }}` (the apex) + `terraria.{{ domain }}`** (`DOMAINS=` in
  the compose) — the public, Cloudflare-proxied A records that track the homelab's dynamic IP.
- **Manually-created, un-managed record — `*.local.<domain>` (grey-cloud / DNS-only):** a
  second, more-specific wildcard in the Cloudflare zone answers every `*.local.<domain>` name with
  the server's LAN IP (`10.0.0.161`) directly (verified: `dig +short foo.local.<domain> @1.1.1.1`
  → `10.0.0.161`). It is **not** managed by this role or any IaC — it's a hand-created zone entry
  and a load-bearing piece of the split-horizon / WireGuard remote-access design
  (`docs/wireguard-private-homelab-access.md`): over the tunnel, `*.local` names resolve straight to
  the LAN without Cloudflare's edge. Low-risk (10.0.0.161 is RFC1918, unroutable from the internet,
  so it bypasses nothing — CrowdSec/Authelia still gate the proxied edge), but recorded here so it
  isn't an orphan: nothing catches it being changed/deleted in the Cloudflare dashboard, and a
  future operator has no other record of why it exists. Pi-hole's `dnsmasq.yml.j2`
  (`address=/local.<domain>/{{ server_ip }}`) is the LAN-side twin for the documented client
  configs. **Do not "fix" the internal-IP disclosure by deleting it without first migrating those
  WireGuard/split-DNS clients** — it's intentional infrastructure, not a leak.
- **Heartbeat monitoring:** each updater pings an Uptime Kuma **push** monitor
  (`UPTIMEKUMA` env) after every successful update — a dead-man's-switch for silent
  failures, since favonia is distroless and can't carry a Docker healthcheck. Detection
  is not instant: a stalled/failing updater only trips the monitor once its Kuma push
  heartbeat window lapses (no success ping arrives), not on the first failed update. The
  push monitors are created by AutoKuma via the `kuma(..., monitor_type='push',
  push_token=...)` macro branch; tokens are `cloudflare_ddns_{proxied,direct}_push_token`
  in `secrets.yml` (we set them — Kuma honors client-supplied tokens). Uses the
  internal `http://uptime-kuma:3001` URL (same `monitoring` net) to keep the
  heartbeat off public DNS and the Authelia-gated route.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "cloudflare-ddns"`
