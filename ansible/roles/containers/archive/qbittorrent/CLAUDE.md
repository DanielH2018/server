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
  **The creds are file-mounted, not inline env** (`FILE__MULLVAD_PRIVATE_KEY`/`FILE__MULLVAD_ACCOUNT`
  → `./mullvad-secrets/{key,account}`, 0600) so these tier-external secrets stay out of the
  container Env the docker-proxy exposes (2026-07-15 review L2). The LSIO base's `init-envfile`
  resolves `FILE__<VAR>` into `<VAR>` before the `wireguard-mullvad` mod reads it (verified s6 order);
  the files are `| trim`med because init-envfile does not strip a trailing newline.
- Watchtower disabled on the wireguard container.
- **Mounts `containers/data/torrents` at `/data/torrents`** (scoped from the whole-tree
  `/data` mount, 2026-07-02 security review — the largest untrusted-input surface shouldn't
  have rw over the media/book library it never touches). Torrents land in
  `data/torrents/{tv-sonarr,radarr,incomplete}` (qBittorrent categories `tv-sonarr`/`radarr`);
  the in-container path matches what Sonarr/Radarr see, so their import paths are unchanged.
  The hardlink requirement lives in **Sonarr/Radarr**, which keep the whole `data` tree in ONE
  mount: `link()` returns `EXDEV` across separate bind mounts even on the same filesystem, so
  *their* mount must span `torrents/` + `media/` — qBittorrent's mount scope is irrelevant to it.
- **Orphaned-netns failure mode:** because the namespace is resolved only at *start*,
  recreating/restarting the wireguard sidecar without also restarting qBittorrent leaves
  qBittorrent serving 8080 in a dead netns — unreachable at `wireguard:8080` (Sonarr/Radarr
  see "Connection refused"). The qBittorrent healthcheck guards against this: it requires
  egress to a public IP (`1.1.1.1`) in addition to the loopback WebUI check, so an orphaned
  (or VPN-down) container goes `unhealthy` and `autoheal` restarts it automatically. A plain
  loopback healthcheck would stay green and hide the outage. Manual fix if ever needed:
  `docker restart qbittorrent`.
- **DNS name for other `media` containers is `wireguard:8080`, NOT `qbittorrent:8080`.** Sharing
  the wireguard netns means qBittorrent has no Docker network record of its own — only `wireguard`
  resolves on `media`. Sonarr/Radarr are already wired this way; use `wireguard:8080` for any new
  download-client consumer (the natural-looking `qbittorrent:8080` silently fails to resolve).
- **UDP-leak-blocked failure mode (zero download progress):** qBittorrent must bind its
  listen interface to **`wg0`**. If it doesn't, libtorrent follows the netns main-table
  default route (`eth0`) and binds its torrent UDP socket there; the Mullvad kill-switch
  then `EPERM`-rejects every UDP tracker / DHT / µTP packet, so peer discovery dies and
  *all* torrents stall at 0% — while TCP egress (and a TCP-only healthcheck) still look
  healthy. `tasks/main.yml` enforces the binding via the WebUI API (`current_network_interface=wg0`,
  empty address so it survives Mullvad IP changes); the healthcheck also asserts a minimum
  DHT node count so a recurrence goes `unhealthy` → `autoheal` restarts → rebinds. A plain
  restart alone does NOT fix it (libtorrent just re-picks `eth0`); the binding must be set.

- **Boot-time systemd unit (`docker-compose-qbittorrent.service`, `tasks/main.yml`) is
  load-bearing — do NOT remove as "dead duplicate lifecycle."** This is the fleet's only
  `network_mode: service:wireguard` sidecar pair, and Compose's health-gated `depends_on`
  ordering (qBittorrent waits for the wireguard healthcheck) is honored ONLY by `docker compose
  up`, never by dockerd's per-container `restart: unless-stopped` at boot. Without the unit, on a
  host reboot qBittorrent can try to join a netns whose wireguard parent isn't up yet and fail to
  *start* — and autoheal can't rescue it (autoheal restarts *unhealthy* containers; a
  never-started one is exited/created, not unhealthy). The unit's `After=docker.service` +
  `ExecStart=docker compose up -d` re-invokes compose at boot to close that race. Same deliberate
  pattern as traefik's `traefik-init.service` (compose-ordering that the restart policy ignores).

## Editing
- Compose: `templates/docker-compose.yml.j2` (the wg0 tunnel config is generated by the
  `wireguard-mullvad` DOCKER_MOD from the `MULLVAD_*` env vars — there is no templated wg0.conf)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "qbittorrent"`
