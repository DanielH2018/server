# wg-easy — WireGuard VPN server + UI (main server)

WireGuard VPN with the wg-easy web admin, for remote access into the homelab.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy`
- **Host:** daniel-server · **Port:** 51821 (UI) · **URL:** `wg-easy.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- The Pi runs a separate `wg-easy-pi` role; the UI here is LAN-bound and behind Authelia
  (a recent hardening change). The WireGuard UDP listen port is published on the host.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "wg-easy"`
