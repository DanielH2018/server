# wg-easy-pi — WireGuard VPN server + UI (Raspberry Pi)

The Pi's WireGuard instance (separate from the main-server `wg-easy` role).
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy`
- **Host:** **daniel-pi** (domain `daniel-pi.com`) · **Port:** 51821 (UI)
- **Authelia:** no
- **Networks:** proxy
- **Depends on:** nothing
- **Config in:** `ansible/inventory/host_vars/daniel-pi.yml` → `containers_list`

## Notable
- **The Pi is LAN-only — never internet-exposed.** Weigh any security finding here as
  LAN-bound. A recent change bound the wg-easy UI to the Pi's LAN IP.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "wg-easy-pi" --limit daniel-pi`
