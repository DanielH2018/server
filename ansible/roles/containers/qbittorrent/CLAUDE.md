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
  confirms `am.i.mullvad.net` connectivity (kill-switch via the `wireguard-mullvad` mod).
- Mullvad creds (`mullvad_account`, `wireguard_interface_private_key`) come from secrets;
  location pinned `us-qas`. WireGuard needs `NET_ADMIN`/`NET_RAW`; qBittorrent does not.
- Watchtower disabled on the wireguard container.

## Editing
- Compose: `templates/docker-compose.yml.j2` · VPN: `templates/wg0.conf.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "qbittorrent"`
