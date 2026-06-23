# wg-easy — WireGuard VPN server + UI

WireGuard VPN with the wg-easy web admin, for remote access into the homelab.
**One host-agnostic role serves both daniel-server and daniel-pi** (the old separate
`wg-easy-pi` role was merged into this one). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy:latest`
- **Hosts:** daniel-server AND daniel-pi
- **UI port:** 51821 · **URL:** `wg-easy.<domain>` (server, behind Authelia) / `http://<pi-lan-ip>:51821` (Pi)
- **WireGuard UDP port:** `udp_port` per host — **51820 on daniel-server, 51822 on daniel-pi**
  (both sit behind one public IP/router, so the listen ports must differ).
- **Networks:** monitoring (server) / proxy (Pi)
- **Config in:** each `ansible/inventory/host_vars/<host>.yml` → `containers_list`

## Notable
- **Exposure is host-driven** via `expose.yml.j2` + `expose_mode`: on the server
  (`expose_mode: traefik`) the UI is routed through Traefik behind Authelia and no host
  port is published; on the Pi (`expose_mode: lan`) the UI is published bound to the Pi's
  LAN IP and emits no Traefik labels. The WireGuard UDP port is always published on the host.
- **Pi UI is unauthenticated on the trusted LAN (accepted risk, 2026-06-23).** The Pi instance
  has no Authelia (LAN-only host) AND wg-easy sets no `PASSWORD`/`PASSWORD_HASH`, so anyone on the
  Pi's LAN can open `http://<pi-lan-ip>:51821` and mint WireGuard client configs (i.e. VPN access
  into the homelab). **Accepted:** the Pi is never internet-exposed and the UI is bound to the Pi's
  LAN IP (not 0.0.0.0, not the tunnel — see the exposure bullet). To gate it later, add a
  `PASSWORD_HASH` env sourced from SOPS to the Pi's `containers_list` wg-easy entry.
- **Built-in healthcheck:** the `wg-easy/wg-easy` image ships its own Docker `HEALTHCHECK`
  (`wg show | grep -q interface` — verifies the WireGuard *interface* is up, not just the
  UI), so there is no compose `healthcheck:` block. `autoheal` and uptime-kuma rely on this
  native status — don't add a redundant probe.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy (server): `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy"`
- Deploy (Pi, driven from the server): `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy" -e target=daniel-pi`
  (`-e target=`, not `--limit` — the play's `hosts:` defaults to the local hostname, so
  `--limit daniel-pi` from the server matches zero hosts)
