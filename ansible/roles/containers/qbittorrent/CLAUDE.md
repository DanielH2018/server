# qbittorrent — BitTorrent client behind WireGuard (Mullvad)

qBittorrent whose traffic is forced through a Mullvad WireGuard sidecar with a
kill-switch. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `lscr.io/linuxserver/qbittorrent:latest` + `lscr.io/linuxserver/wireguard:latest`
- **Host:** daniel-server · **Port:** 8080 (WebUI) · **URL:** `qbittorrent.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **qBittorrent uses `network_mode: service:wireguard`** — it has no network of its own;
  all traffic egresses through the VPN. It starts only after the wireguard healthcheck
  confirms tunnel egress (reaches `1.1.1.1` through `wg0`; kill-switch via the
  `wireguard-mullvad` mod guarantees that egress is Mullvad-only).
- Mullvad creds (`mullvad_account`, `wireguard_interface_private_key`) come from secrets;
  location pinned `us-qas`. WireGuard needs `NET_ADMIN`/`NET_RAW`; qBittorrent does not.
- Watchtower disabled on the wireguard container.
- **Orphaned-netns failure mode:** because the namespace is resolved only at *start*,
  recreating/restarting the wireguard sidecar without also restarting qBittorrent leaves
  qBittorrent serving 8080 in a dead netns — unreachable at `wireguard:8080` (Sonarr/Radarr
  see "Connection refused"). The qBittorrent healthcheck guards against this: it requires
  egress to a public IP (`1.1.1.1`) in addition to the loopback WebUI check, so an orphaned
  (or VPN-down) container goes `unhealthy` and `autoheal` restarts it automatically. A plain
  loopback healthcheck would stay green and hide the outage. Manual fix if ever needed:
  `docker restart qbittorrent`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · VPN: `templates/wg0.conf.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "qbittorrent"`
