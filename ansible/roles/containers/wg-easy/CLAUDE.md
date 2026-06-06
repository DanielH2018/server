# wg-easy — WireGuard VPN server + UI (main server)

WireGuard VPN with the wg-easy web admin, for remote access into the homelab.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy:latest`
- **Host:** daniel-server · **Port:** 51821 (UI) · **URL:** `wg-easy.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- The Pi runs a separate `wg-easy-pi` role; the UI here is LAN-bound and behind Authelia
  (a recent hardening change). The WireGuard UDP listen port is published on the host.
- **Built-in healthcheck:** the `wg-easy/wg-easy` image ships its own Docker `HEALTHCHECK`
  (`wg show | grep -q interface` — verifies the WireGuard *interface* is actually up, not
  just that the UI responds), so Docker reports container health without a `healthcheck:`
  block in the compose template. `autoheal` and uptime-kuma rely on this native status —
  don't add a redundant compose `healthcheck` (and don't "improve" it to a UI probe; the
  interface check is the stronger signal). The Pi's `wg-easy-pi` shares this image and
  healthcheck.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "wg-easy"`
